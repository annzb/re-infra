from __future__ import annotations

import json

import pytest

from rc_infra.apply import ApplyRefused, apply_plan
from rc_infra.aws import ChangeSetKind
from rc_infra.env_config import EnvConfig
from rc_infra.planner import build_plan
from rc_infra.templates import PLATFORM_TEMPLATE_PATH, load_template, platform_import_targets
from tests.fakes import (
    FakeAws,
    add_removed_environment,
    bucket_name,
    env_stack_tags,
    matching_live_config,
)


def _quiet(_: str) -> None:
    pass


def test_a_blocked_protected_environment_applies_nothing(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-dev", status="UPDATE_ROLLBACK_FAILED", tags=env_stack_tags("dev"))
    plan = build_plan(env_config, fake.aws)

    with pytest.raises(ApplyRefused, match="dev"):
        apply_plan(plan, env_config, fake.aws, log=_quiet)
    assert fake.events == []


def test_a_blocked_platform_applies_nothing(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-platform", status="UPDATE_ROLLBACK_FAILED")
    plan = build_plan(env_config, fake.aws)

    with pytest.raises(ApplyRefused, match="platform"):
        apply_plan(plan, env_config, fake.aws, log=_quiet)
    assert fake.events == []


def test_a_blocked_preview_is_skipped_and_the_rest_applies(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-preview1", status="UPDATE_ROLLBACK_FAILED", tags=env_stack_tags("preview1"))
    plan = build_plan(env_config, fake.aws)

    result = apply_plan(plan, env_config, fake.aws, log=_quiet)

    assert result.failed == ["preview1: blocked"]
    assert "preview1" not in result.completed
    assert "prod" in result.completed and "platform" in result.completed
    assert "rc-env-preview1" not in {d["stack"] for d in fake.stacks.deploys}


def test_creates_everything_and_protects_persistent_stacks(env_config: EnvConfig, fake: FakeAws) -> None:
    result = apply_plan(build_plan(env_config, fake.aws), env_config, fake.aws, log=_quiet)

    assert result.failed == []
    assert {d["kind"] for d in fake.stacks.deploys} == {ChangeSetKind.CREATE}
    assert {d["stack"] for d in fake.stacks.deploys} == {"rc-platform"} | {env.core_stack for env in env_config.environments}
    protected = {e[1] for e in fake.events if e[0] == "termination_protection"}
    assert protected == {"rc-platform", "rc-env-prod", "rc-env-staging", "rc-env-dev"}
    prod_deploy = next(d for d in fake.stacks.deploys if d["stack"] == "rc-env-prod")
    assert prod_deploy["tags"] == env_stack_tags("prod")
    assert prod_deploy["parameters"] == {"EnvironmentName": "prod"}


def test_import_runs_before_full_update(env_config: EnvConfig, fake: FakeAws) -> None:
    existing = ("embeddings", "user-corpus", "avatars", "recordings")
    for purpose in existing:
        fake.buckets.live[bucket_name("prod", purpose)] = matching_live_config(purpose)

    apply_plan(build_plan(env_config, fake.aws), env_config, fake.aws, log=_quiet)

    prod = [d for d in fake.stacks.deploys if d["stack"] == "rc-env-prod"]
    assert [d["kind"] for d in prod] == [ChangeSetKind.IMPORT, ChangeSetKind.UPDATE]
    import_template = json.loads(prod[0]["template_body"])
    assert set(import_template["Resources"]) == {
        "EmbeddingsBucket",
        "UserCorpusBucket",
        "AvatarsBucket",
        "RecordingsBucket",
    }
    assert {r["ResourceType"] for r in prod[0]["resources_to_import"]} == {"AWS::S3::Bucket"}
    assert {r["ResourceIdentifier"]["BucketName"] for r in prod[0]["resources_to_import"]} == {bucket_name("prod", purpose) for purpose in existing}
    assert "SchemaDumpsBucket" in json.loads(prod[1]["template_body"])["Resources"]


def test_rolled_back_stack_is_deleted_before_create(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-preview3", status="ROLLBACK_COMPLETE", tags=env_stack_tags("preview3"))

    apply_plan(build_plan(env_config, fake.aws), env_config, fake.aws, log=_quiet)

    preview3 = [e for e in fake.events if "rc-env-preview3" in e]
    assert preview3 == [
        ("delete_stack", "rc-env-preview3"),
        ("deploy", "CREATE", "rc-env-preview3"),
    ]


def test_one_failure_does_not_stop_others_and_deletes_run_last(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    fake.stacks.fail_deploy.add("rc-env-preview3")

    result = apply_plan(build_plan(env_config, fake.aws), env_config, fake.aws, log=_quiet)

    assert [f.split(":")[0] for f in result.failed] == ["preview3"]
    assert "rc-env-preview89" in fake.stacks.stacks
    assert fake.events[-1] == ("delete_stack", "rc-env-preview42")
    first_delete = next(i for i, e in enumerate(fake.events) if e[0].startswith("delete"))
    assert all(e[0] != "deploy" for e in fake.events[first_delete:])


def test_platform_import_carries_each_resource_type(env_config: EnvConfig, fake: FakeAws) -> None:
    """The import change set must name every type, not assume S3 as it once did."""
    template = load_template(PLATFORM_TEMPLATE_PATH)
    adopted = [t for t in platform_import_targets(env_config, template) if t.resource_type != "AWS::ECR::Repository"]
    for target in adopted:
        fake.resources.add(target.resource_type, target.identifier)

    apply_plan(build_plan(env_config, fake.aws), env_config, fake.aws, log=_quiet)

    platform = [d for d in fake.stacks.deploys if d["stack"] == "rc-platform"]
    assert [d["kind"] for d in platform] == [ChangeSetKind.IMPORT, ChangeSetKind.UPDATE]

    sent = {r["LogicalResourceId"]: r for r in platform[0]["resources_to_import"]}
    assert sent["ProdUserPool"]["ResourceType"] == "AWS::Cognito::UserPool"
    assert sent["ProdUserPool"]["ResourceIdentifier"] == {"UserPoolId": "us-east-1_1GIFBpLKf"}
    assert sent["LambdaPowerRole"]["ResourceType"] == "AWS::IAM::Role"
    assert sent["PreviewUserPoolDomain"]["ResourceIdentifier"]["Domain"] == "rc-preview-v2"

    # An import change set may only contain the resources being imported; the new
    # repositories arrive in the update that follows.
    imported = json.loads(platform[0]["template_body"])["Resources"]
    assert set(imported) == set(sent)
    assert "LambdaBaseImageRepository" in json.loads(platform[1]["template_body"])["Resources"]
