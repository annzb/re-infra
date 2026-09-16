from __future__ import annotations

import json

import pytest

from rc_infra.apply import ApplyRefused, apply_plan
from rc_infra.aws import ChangeSetKind
from rc_infra.catalog import Catalog
from rc_infra.planner import build_plan
from tests.fakes import (
    FakeAws,
    add_removed_environment,
    bucket_name,
    env_stack_tags,
    matching_live_config,
)


def _quiet(_: str) -> None:
    pass


def test_blocked_plan_applies_nothing(catalog: Catalog, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-dev", status="UPDATE_ROLLBACK_FAILED", tags=env_stack_tags("dev"))
    plan = build_plan(catalog, fake.aws)

    with pytest.raises(ApplyRefused, match="dev"):
        apply_plan(plan, catalog, fake.aws, log=_quiet)
    assert fake.events == []


def test_creates_everything_and_protects_persistent_stacks(catalog: Catalog, fake: FakeAws) -> None:
    result = apply_plan(build_plan(catalog, fake.aws), catalog, fake.aws, log=_quiet)

    assert result.failed == []
    assert {d["kind"] for d in fake.stacks.deploys} == {ChangeSetKind.CREATE}
    assert {d["stack"] for d in fake.stacks.deploys} == {"rc-platform"} | {
        env.core_stack for env in catalog.environments
    }
    protected = {e[1] for e in fake.events if e[0] == "termination_protection"}
    assert protected == {"rc-platform", "rc-env-prod", "rc-env-staging", "rc-env-dev"}
    prod_deploy = next(d for d in fake.stacks.deploys if d["stack"] == "rc-env-prod")
    assert prod_deploy["tags"] == env_stack_tags("prod")
    assert prod_deploy["parameters"]["UserPoolId"] == "us-east-1_1GIFBpLKf"
    assert prod_deploy["role_arn"].endswith(":role/rc-infra-cfn-exec")


def test_import_runs_before_full_update(catalog: Catalog, fake: FakeAws) -> None:
    existing = ("embeddings", "user-corpus", "avatars", "recordings")
    for purpose in existing:
        fake.buckets.live[bucket_name("prod", purpose)] = matching_live_config(purpose)

    apply_plan(build_plan(catalog, fake.aws), catalog, fake.aws, log=_quiet)

    prod = [d for d in fake.stacks.deploys if d["stack"] == "rc-env-prod"]
    assert [d["kind"] for d in prod] == [ChangeSetKind.IMPORT, ChangeSetKind.UPDATE]
    import_template = json.loads(prod[0]["template_body"])
    assert set(import_template["Resources"]) == {
        "EmbeddingsBucket",
        "UserCorpusBucket",
        "AvatarsBucket",
        "RecordingsBucket",
    }
    assert {r["ResourceIdentifier"]["BucketName"] for r in prod[0]["resources_to_import"]} == {
        bucket_name("prod", purpose) for purpose in existing
    }
    assert "SchemaDumpsBucket" in json.loads(prod[1]["template_body"])["Resources"]


def test_rolled_back_stack_is_deleted_before_create(catalog: Catalog, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-preview3", status="ROLLBACK_COMPLETE", tags=env_stack_tags("preview3"))

    apply_plan(build_plan(catalog, fake.aws), catalog, fake.aws, log=_quiet)

    preview3 = [e for e in fake.events if "rc-env-preview3" in e]
    assert preview3 == [
        ("delete_stack", "rc-env-preview3"),
        ("deploy", "CREATE", "rc-env-preview3"),
    ]


def test_one_failure_does_not_stop_others_and_deletes_run_last(
    catalog: Catalog, fake: FakeAws
) -> None:
    add_removed_environment(fake, "preview42")
    fake.stacks.fail_deploy.add("rc-env-preview3")

    result = apply_plan(build_plan(catalog, fake.aws), catalog, fake.aws, log=_quiet)

    assert [f.split(":")[0] for f in result.failed] == ["preview3"]
    assert "rc-env-preview89" in fake.stacks.stacks
    assert fake.events[-1] == ("delete_stack", "rc-env-preview42")
    first_delete = next(i for i, e in enumerate(fake.events) if e[0].startswith("delete"))
    assert all(e[0] != "deploy" for e in fake.events[first_delete:])
