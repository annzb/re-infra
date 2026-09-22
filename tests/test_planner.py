from __future__ import annotations

from typing import Any

from rc_infra.aws import ResourceChange
from rc_infra.env_config import EnvConfig
from rc_infra.planner import PLATFORM_TARGET, Action, ActionKind, Plan, build_plan, render_text
from rc_infra.templates import PLATFORM_TEMPLATE_PATH, load_template, platform_import_targets
from tests.fakes import (
    FakeAws,
    add_removed_environment,
    bucket_name,
    dynamo_sync_tags,
    env_stack_tags,
    matching_live_config,
)

PROD_EXISTING = ("embeddings", "user-corpus", "avatars", "recordings")


def _by_target(plan: Plan) -> dict[str, Action]:
    return {action.target: action for action in plan.actions}


def _all_stacks_exist(fake: FakeAws, env_config: EnvConfig) -> None:
    fake.stacks.add("rc-platform", tags={"ManagedBy": "re-infra", "Component": "platform"})
    for env in env_config.environments:
        fake.stacks.add(env.core_stack, tags=env_stack_tags(env.name))


def test_empty_account_creates_everything(env_config: EnvConfig, fake: FakeAws) -> None:
    plan = build_plan(env_config, fake.aws)
    actions = _by_target(plan)
    assert set(actions) == {"platform", *env_config.names}
    assert {action.kind for action in plan.actions} == {ActionKind.CREATE}
    assert fake.events == []


def test_existing_matching_buckets_are_imported(env_config: EnvConfig, fake: FakeAws) -> None:
    for purpose in PROD_EXISTING:
        fake.buckets.live[bucket_name("prod", purpose)] = matching_live_config(purpose)

    action = _by_target(build_plan(env_config, fake.aws))["prod"]

    assert action.kind is ActionKind.IMPORT
    assert {target.logical_id: target.describe for target in action.imports} == {
        "EmbeddingsBucket": bucket_name("prod", "embeddings"),
        "UserCorpusBucket": bucket_name("prod", "user-corpus"),
        "AvatarsBucket": bucket_name("prod", "avatars"),
        "RecordingsBucket": bucket_name("prod", "recordings"),
    }
    assert {target.resource_type for target in action.imports} == {"AWS::S3::Bucket"}
    assert action.imports[0].identifier == {"BucketName": action.imports[0].describe}
    assert set(action.details) == {
        f"then create bucket {bucket_name('prod', 'schema-dumps')}",
        f"then create bucket {bucket_name('prod', 'property-registry')}",
    }


def test_mismatched_existing_bucket_blocks(env_config: EnvConfig, fake: FakeAws) -> None:
    for purpose in PROD_EXISTING:
        fake.buckets.live[bucket_name("prod", purpose)] = matching_live_config(purpose)
    fake.buckets.live[bucket_name("prod", "user-corpus")]["lifecycle"] = None

    plan = build_plan(env_config, fake.aws)
    action = _by_target(plan)["prod"]

    assert action.kind is ActionKind.BLOCKED
    assert any(bucket_name("prod", "user-corpus") in d and "lifecycle" in d for d in action.details)
    assert plan.blocked == [action]


def test_cors_difference_is_advisory_and_still_imports(env_config: EnvConfig, fake: FakeAws) -> None:
    """An older CORS rule must not hold up an adoption; the update reconciles it."""
    for purpose in PROD_EXISTING:
        fake.buckets.live[bucket_name("prod", purpose)] = matching_live_config(purpose)
    name = bucket_name("prod", "avatars")
    fake.buckets.live[name]["cors"] = {"CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["*"]}]}

    action = _by_target(build_plan(env_config, fake.aws))["prod"]

    assert action.kind is ActionKind.IMPORT
    assert any(name in detail and "reconcile" in detail for detail in action.details)


def test_bucket_owned_by_another_stack_blocks(env_config: EnvConfig, fake: FakeAws) -> None:
    name = bucket_name("dev", "avatars")
    fake.buckets.live[name] = matching_live_config("avatars")
    fake.buckets.owners[name] = "rc-dev"

    action = _by_target(build_plan(env_config, fake.aws))["dev"]

    assert action.kind is ActionKind.BLOCKED
    assert f"bucket {name} already belongs to stack rc-dev" in action.details


def test_existing_stacks_update_or_noop(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-env-dev"] = [ResourceChange("Modify", "AvatarsBucket", "AWS::S3::Bucket")]

    actions = _by_target(build_plan(env_config, fake.aws))

    assert actions["dev"].kind is ActionKind.UPDATE
    assert actions["dev"].details == ("Modify AvatarsBucket [AWS::S3::Bucket]",)
    assert actions["prod"].kind is ActionKind.NOOP
    assert actions["platform"].kind is ActionKind.NOOP


def test_busy_or_broken_stack_blocks(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-dev", status="UPDATE_IN_PROGRESS", tags=env_stack_tags("dev"))
    fake.stacks.add("rc-env-staging", status="UPDATE_ROLLBACK_FAILED", tags=env_stack_tags("staging"))

    actions = _by_target(build_plan(env_config, fake.aws))

    assert actions["dev"].kind is ActionKind.BLOCKED
    assert actions["staging"].kind is ActionKind.BLOCKED


def test_rolled_back_stack_is_recreated(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-preview3", status="ROLLBACK_COMPLETE", tags=env_stack_tags("preview3"))

    action = _by_target(build_plan(env_config, fake.aws))["preview3"]

    assert action.kind is ActionKind.CREATE
    assert action.replace_failed_stack


def test_removed_environment_is_deleted(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    add_removed_environment(fake, "preview42")
    fake.tables.table_tags["rc-preview420-users"] = dynamo_sync_tags("preview420")
    fake.stacks.add("rc-preview42")  # legacy app stack

    plan = build_plan(env_config, fake.aws)
    action = _by_target(plan)["preview42"]

    assert action.kind is ActionKind.DELETE
    details = "\n".join(action.details)
    assert "delete app stack rc-app-preview42" in details
    assert "delete table rc-preview42-users" in details
    assert "delete table rc-preview42-messages" in details
    assert "skip table rc-preview42-legacy" in details
    assert "rc-preview420-users" not in details
    assert f"empty and delete bucket {bucket_name('preview42', 'avatars')}" in details
    assert "legacy app stack rc-preview42 is not managed here and is kept" in details
    assert action.details.index("delete stack rc-env-preview42") > max(
        i for i, d in enumerate(action.details) if d.startswith("empty and delete bucket")
    )
    assert render_text(plan).startswith("WARNING: applying deletes 1 environment(s): preview42, including their data.")
    assert fake.events == []


def test_termination_protected_removed_stack_blocks(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    stack: Any = fake.stacks.stacks["rc-env-preview42"]
    fake.stacks.add(stack.name, tags=stack.tags, termination_protection=True)

    action = _by_target(build_plan(env_config, fake.aws))["preview42"]

    assert action.kind is ActionKind.BLOCKED
    assert "termination protection" in action.details[0]


def test_unmanaged_core_stack_is_ignored(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-handmade", tags={})

    plan = build_plan(env_config, fake.aws)

    assert "handmade" not in _by_target(plan)
    assert plan.notes == ("ignoring rc-env-handmade: not tagged ManagedBy=re-infra",)


def test_blocked_and_deleted_are_listed_first(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    fake.stacks.add("rc-env-dev", status="UPDATE_ROLLBACK_FAILED", tags=env_stack_tags("dev"))

    plan = build_plan(env_config, fake.aws)
    kinds = [action.kind for action in plan.actions]

    assert kinds[:2] == [ActionKind.BLOCKED, ActionKind.DELETE]
    lines = render_text(plan).splitlines()
    assert lines[0].startswith("WARNING: applying deletes 1 environment(s): preview42")
    assert lines[2].startswith("WARNING: dev is blocked")
    assert lines[4].startswith("BLOCKED  dev")


# ── rc-platform adoption ────────────────────────────────────────────

PLATFORM_TEMPLATE = load_template(PLATFORM_TEMPLATE_PATH)


def _identity_exists(fake: FakeAws, env_config: EnvConfig) -> list[Any]:
    """Mark every Cognito resource and the shared role as already live."""
    targets = [t for t in platform_import_targets(env_config, PLATFORM_TEMPLATE) if t.resource_type != "AWS::ECR::Repository"]
    for target in targets:
        fake.resources.add(target.resource_type, target.identifier)
    return targets


def test_platform_adopts_existing_identity_resources(env_config: EnvConfig, fake: FakeAws) -> None:
    expected = _identity_exists(fake, env_config)

    action = _by_target(build_plan(env_config, fake.aws))[PLATFORM_TARGET]

    assert action.kind is ActionKind.IMPORT
    assert {t.logical_id for t in action.imports} == {t.logical_id for t in expected}
    # The repositories do not exist, so they are created by the update that follows.
    assert set(action.details) == {
        "then create LambdaBaseImageRepository",
        "then create ApiImageRepository",
        "then create MatchingImageRepository",
    }


def test_platform_import_carries_real_identifiers(env_config: EnvConfig, fake: FakeAws) -> None:
    _identity_exists(fake, env_config)

    action = _by_target(build_plan(env_config, fake.aws))[PLATFORM_TARGET]
    by_id = {t.logical_id: t for t in action.imports}

    assert by_id["ProdUserPool"].identifier == {"UserPoolId": "us-east-1_1GIFBpLKf"}
    assert by_id["ProdUserPoolClient"].identifier == {
        "UserPoolId": "us-east-1_1GIFBpLKf",
        "ClientId": "10248ltom7g0tgb22si919e9ak",
    }
    # The domain identifier is the prefix from the template, not envs.yaml's hostname.
    assert by_id["ProdUserPoolDomain"].identifier == {"UserPoolId": "us-east-1_1GIFBpLKf", "Domain": "rc-prod-v2"}
    assert by_id["LambdaPowerRole"].identifier == {"RoleName": "LambdaPower"}


def test_platform_creates_when_nothing_exists(env_config: EnvConfig, fake: FakeAws) -> None:
    action = _by_target(build_plan(env_config, fake.aws))[PLATFORM_TARGET]
    assert action.kind is ActionKind.CREATE
    assert action.imports == ()


def test_replacing_a_user_pool_is_blocked(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-platform"] = [
        ResourceChange("Modify", "ProdUserPool", "AWS::Cognito::UserPool", replacement="True"),
    ]

    plan = build_plan(env_config, fake.aws)
    action = _by_target(plan)[PLATFORM_TARGET]

    assert action.kind is ActionKind.BLOCKED
    assert any("ProdUserPool" in detail for detail in action.details)
    # The platform is shared, so this must stop the whole apply rather than skip.
    assert plan.halting == [action]


def test_removing_a_user_pool_is_blocked(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-platform"] = [ResourceChange("Remove", "DevUserPoolDomain", "AWS::Cognito::UserPoolDomain")]

    assert _by_target(build_plan(env_config, fake.aws))[PLATFORM_TARGET].kind is ActionKind.BLOCKED


def test_in_place_identity_changes_are_allowed(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-platform"] = [
        ResourceChange("Modify", "ProdUserPoolClient", "AWS::Cognito::UserPoolClient", replacement="False"),
    ]

    assert _by_target(build_plan(env_config, fake.aws))[PLATFORM_TARGET].kind is ActionKind.UPDATE


def test_a_blocked_preview_does_not_halt_the_plan(env_config: EnvConfig, fake: FakeAws) -> None:
    name = bucket_name("preview1", "avatars")
    fake.buckets.live[name] = matching_live_config("avatars")
    fake.buckets.owners[name] = "rc-preview1"

    plan = build_plan(env_config, fake.aws)

    assert _by_target(plan)["preview1"].kind is ActionKind.BLOCKED
    assert plan.halting == []
    assert "skipped" in render_text(plan)
