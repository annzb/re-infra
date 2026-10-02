from __future__ import annotations

from typing import Any

from rc_infra.aws import ResourceChange
from rc_infra.env_config import EnvConfig
from rc_infra.planner import IDENTITY_TARGET, PLATFORM_TARGET, Action, ActionKind, Plan, build_plan, render_text
from tests.fakes import (
    FakeAws,
    add_removed_environment,
    bucket_name,
    dynamo_sync_tags,
    env_stack_tags,
)


def _by_target(plan: Plan) -> dict[str, Action]:
    return {action.target: action for action in plan.actions}


def _all_stacks_exist(fake: FakeAws, env_config: EnvConfig) -> None:
    fake.stacks.add("rc-platform", tags={"ManagedBy": "re-infra", "Component": "platform"})
    fake.stacks.add("rc-identity", tags={"ManagedBy": "re-infra", "Component": "identity"})
    for env in env_config.environments:
        fake.stacks.add(env.core_stack, tags=env_stack_tags(env.name))


def test_empty_account_creates_everything(env_config: EnvConfig, fake: FakeAws) -> None:
    plan = build_plan(env_config, fake.aws)
    actions = _by_target(plan)
    assert set(actions) == {PLATFORM_TARGET, IDENTITY_TARGET, *env_config.names}
    assert {action.kind for action in plan.actions} == {ActionKind.CREATE}
    assert actions[IDENTITY_TARGET].stack == "rc-identity"
    assert fake.events == []


def test_unrelated_buckets_never_change_the_plan(env_config: EnvConfig, fake: FakeAws) -> None:
    """A bucket from an older generation is not this repository's business."""
    fake.buckets.live["rc-prod-avatars-273268178059"] = 12

    assert _by_target(build_plan(env_config, fake.aws))["prod"].kind is ActionKind.CREATE


def test_no_action_can_import() -> None:
    assert "IMPORT" not in ActionKind.__members__


def test_existing_stacks_update_or_noop(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-env-dev"] = [ResourceChange("Modify", "AvatarsBucket", "AWS::S3::Bucket")]

    actions = _by_target(build_plan(env_config, fake.aws))

    assert actions["dev"].kind is ActionKind.UPDATE
    assert actions["dev"].details == ("Modify AvatarsBucket [AWS::S3::Bucket]",)
    assert actions["prod"].kind is ActionKind.NOOP
    assert actions[PLATFORM_TARGET].kind is ActionKind.NOOP
    assert actions[IDENTITY_TARGET].kind is ActionKind.NOOP


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


# ── shared stacks ──────────────────────────────────────────────────


def test_replacing_a_user_pool_is_blocked(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-identity"] = [
        ResourceChange("Modify", "ProdUserPool", "AWS::Cognito::UserPool", replacement="True"),
    ]

    plan = build_plan(env_config, fake.aws)
    action = _by_target(plan)[IDENTITY_TARGET]

    assert action.kind is ActionKind.BLOCKED
    assert any("ProdUserPool" in detail for detail in action.details)
    # Identity is shared, so this must stop the whole apply rather than skip.
    assert plan.halting == [action]


def test_replacing_the_shared_role_is_blocked(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-platform"] = [
        ResourceChange("Modify", "SharedLambdaExecutionRole", "AWS::IAM::Role", replacement="Conditional"),
    ]

    plan = build_plan(env_config, fake.aws)

    assert plan.halting == [_by_target(plan)[PLATFORM_TARGET]]


def test_removing_a_user_pool_is_blocked(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-identity"] = [ResourceChange("Remove", "DevUserPoolDomain", "AWS::Cognito::UserPoolDomain")]

    assert _by_target(build_plan(env_config, fake.aws))[IDENTITY_TARGET].kind is ActionKind.BLOCKED


def test_in_place_identity_changes_are_allowed(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-identity"] = [
        ResourceChange("Modify", "ProdUserPoolClient", "AWS::Cognito::UserPoolClient", replacement="False"),
    ]

    assert _by_target(build_plan(env_config, fake.aws))[IDENTITY_TARGET].kind is ActionKind.UPDATE


def test_a_blocked_preview_does_not_halt_the_plan(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-preview1", status="UPDATE_ROLLBACK_FAILED", tags=env_stack_tags("preview1"))

    plan = build_plan(env_config, fake.aws)

    assert _by_target(plan)["preview1"].kind is ActionKind.BLOCKED
    assert plan.halting == []
    assert "skipped" in render_text(plan)
