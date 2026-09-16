from __future__ import annotations

from typing import Any

from rc_infra.aws import ResourceChange
from rc_infra.env_config import EnvConfig
from rc_infra.planner import Action, ActionKind, Plan, build_plan, render_text
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
    assert dict(action.imports) == {
        "EmbeddingsBucket": bucket_name("prod", "embeddings"),
        "UserCorpusBucket": bucket_name("prod", "user-corpus"),
        "AvatarsBucket": bucket_name("prod", "avatars"),
        "RecordingsBucket": bucket_name("prod", "recordings"),
    }
    assert action.details == (f"then create bucket {bucket_name('prod', 'schema-dumps')}",)


def test_mismatched_existing_bucket_blocks(env_config: EnvConfig, fake: FakeAws) -> None:
    for purpose in PROD_EXISTING:
        fake.buckets.live[bucket_name("prod", purpose)] = matching_live_config(purpose)
    fake.buckets.live[bucket_name("prod", "user-corpus")]["cors"] = None

    plan = build_plan(env_config, fake.aws)
    action = _by_target(plan)["prod"]

    assert action.kind is ActionKind.BLOCKED
    assert any(bucket_name("prod", "user-corpus") in d and "cors" in d for d in action.details)
    assert plan.blocked == [action]


def test_bucket_owned_by_another_stack_blocks(env_config: EnvConfig, fake: FakeAws) -> None:
    name = bucket_name("dev", "avatars")
    fake.buckets.live[name] = matching_live_config("avatars")
    fake.buckets.owners[name] = "rc-dev"

    action = _by_target(build_plan(env_config, fake.aws))["dev"]

    assert action.kind is ActionKind.BLOCKED
    assert f"bucket {name} already belongs to stack rc-dev" in action.details


def test_existing_stacks_update_or_noop(env_config: EnvConfig, fake: FakeAws) -> None:
    _all_stacks_exist(fake, env_config)
    fake.stacks.previews["rc-env-dev"] = [ResourceChange("Modify", "RegionParameter", "AWS::SSM::Parameter")]

    actions = _by_target(build_plan(env_config, fake.aws))

    assert actions["dev"].kind is ActionKind.UPDATE
    assert actions["dev"].details == ("Modify RegionParameter [AWS::SSM::Parameter]",)
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
    assert lines[2].startswith("BLOCKED  dev")
