from __future__ import annotations

import pytest

from rc_infra.aws import StackResource
from rc_infra.env_config import EnvConfig
from rc_infra.teardown import TeardownRefused, check_deletable, teardown
from tests.fakes import (
    FakeAws,
    add_removed_environment,
    bucket_name,
    dynamo_sync_tags,
    env_stack_tags,
)


def _quiet(_: str) -> None:
    pass


def test_deletes_in_safe_order(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")

    teardown("preview42", fake.aws, env_config.names, log=_quiet)

    kinds = [event[0] for event in fake.events]
    assert fake.events[0] == ("delete_stack", "rc-app-preview42")
    assert fake.events[-1] == ("delete_stack", "rc-env-preview42")
    last_table = max(i for i, kind in enumerate(kinds) if kind == "delete_table")
    assert last_table < kinds.index("delete_bucket")
    assert set(fake.tables.table_tags) == {"rc-preview42-legacy"}
    assert fake.buckets.live == {}


@pytest.mark.parametrize("name", ["prod", "staging", "dev"])
def test_protected_environments_are_refused(env_config: EnvConfig, fake: FakeAws, name: str) -> None:
    stack = fake.stacks.add(f"rc-env-{name}", tags=env_stack_tags(name))
    with pytest.raises(TeardownRefused, match="protected"):
        check_deletable(name, stack, env_config.names - {name})


def test_declared_environment_is_refused(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview3")
    with pytest.raises(TeardownRefused, match="still declared"):
        teardown("preview3", fake.aws, env_config.names, log=_quiet)
    assert fake.events == []


def test_untagged_stack_is_refused(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    fake.stacks.add("rc-env-preview42", tags={"ManagedBy": "someone-else"})
    with pytest.raises(TeardownRefused, match="not managed by re-infra"):
        teardown("preview42", fake.aws, env_config.names, log=_quiet)
    assert fake.events == []


def test_stack_tagged_for_another_environment_is_refused(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    fake.stacks.add("rc-env-preview42", tags=env_stack_tags("preview4"))
    with pytest.raises(TeardownRefused):
        teardown("preview42", fake.aws, env_config.names, log=_quiet)
    assert fake.events == []


def test_termination_protected_stack_is_refused(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    fake.stacks.add("rc-env-preview42", tags=env_stack_tags("preview42"), termination_protection=True)
    with pytest.raises(TeardownRefused, match="termination protection"):
        teardown("preview42", fake.aws, env_config.names, log=_quiet)
    assert fake.events == []


def test_bucket_outside_environment_prefix_is_refused(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    fake.stacks.resources["rc-env-preview42"].append(StackResource("Stray", bucket_name("prod", "embeddings"), "AWS::S3::Bucket"))
    with pytest.raises(TeardownRefused, match="does not start with rc-preview42-"):
        teardown("preview42", fake.aws, env_config.names, log=_quiet)
    assert fake.events == []


def test_tables_of_other_environments_are_kept(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    fake.tables.table_tags["rc-preview42-wrongenv"] = dynamo_sync_tags("preview4")
    fake.tables.table_tags["rc-preview420-users"] = dynamo_sync_tags("preview420")

    teardown("preview42", fake.aws, env_config.names, log=_quiet)

    assert {"rc-preview42-wrongenv", "rc-preview420-users"} <= set(fake.tables.table_tags)


def test_retry_after_partial_failure_completes(env_config: EnvConfig, fake: FakeAws) -> None:
    add_removed_environment(fake, "preview42")
    fake.buckets.fail_delete_once.add(bucket_name("preview42", "recordings"))

    with pytest.raises(RuntimeError):
        teardown("preview42", fake.aws, env_config.names, log=_quiet)
    assert "rc-env-preview42" in fake.stacks.stacks  # the marker survives the failure

    teardown("preview42", fake.aws, env_config.names, log=_quiet)

    assert "rc-env-preview42" not in fake.stacks.stacks
    assert "rc-app-preview42" not in fake.stacks.stacks
    assert fake.buckets.live == {}


def test_missing_stack_is_a_noop(env_config: EnvConfig, fake: FakeAws) -> None:
    teardown("preview42", fake.aws, env_config.names, log=_quiet)
    assert fake.events == []
