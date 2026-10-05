from __future__ import annotations

import json

from rc_infra.env_config import EnvConfig
from rc_infra.status import ResourceStatus, State, environment_status, render_json, render_text
from tests.fakes import FakeAws, bucket_name, dynamo_sync_tags, env_stack_outputs, env_stack_tags


def _deployed(fake: FakeAws, environment: str) -> None:
    fake.stacks.add(f"rc-env-{environment}", tags=env_stack_tags(environment), outputs=env_stack_outputs(environment))
    for name in env_stack_outputs(environment).values():
        if not name.startswith("arn:"):
            fake.buckets.live[name] = 0


def _states(statuses: list[ResourceStatus]) -> dict[str, State]:
    return {s.resource: s.state for s in statuses}


def test_missing_stack_and_tables_are_not_created(env_config: EnvConfig, fake: FakeAws) -> None:
    statuses = environment_status(env_config.get("dev"), fake.aws)

    assert set(_states(statuses).values()) == {State.NOT_CREATED}
    assert {s.kind for s in statuses} == {"bucket", "table"}


def test_bucket_and_table_emptiness(env_config: EnvConfig, fake: FakeAws) -> None:
    _deployed(fake, "dev")
    fake.buckets.live[bucket_name("dev", "avatars")] = 3
    fake.tables.table_tags["rc2-dev-users"] = dynamo_sync_tags("dev")
    fake.tables.table_tags["rc2-dev-messages"] = dynamo_sync_tags("dev")
    fake.tables.items["rc2-dev-messages"] = 1
    fake.tables.table_tags["rc2-dev-unmanaged"] = {}

    states = _states(environment_status(env_config.get("dev"), fake.aws))

    assert states["AvatarsBucket"] is State.NON_EMPTY
    assert states["UserCorpusBucket"] is State.EMPTY
    assert states["users"] is State.EMPTY
    assert states["messages"] is State.NON_EMPTY
    assert "unmanaged" not in states


def test_old_versions_make_the_versioned_bucket_non_empty(env_config: EnvConfig, fake: FakeAws) -> None:
    _deployed(fake, "dev")
    fake.buckets.versions[bucket_name("dev", "property-registry")] = 2
    fake.buckets.versions[bucket_name("dev", "recordings")] = 2  # not versioned: never asked

    states = _states(environment_status(env_config.get("dev"), fake.aws))

    assert states["PropertyRegistryBucket"] is State.NON_EMPTY
    assert states["RecordingsBucket"] is State.EMPTY


def test_rendering_points_at_manual_transfer_without_naming_a_source(env_config: EnvConfig, fake: FakeAws) -> None:
    _deployed(fake, "dev")
    statuses = environment_status(env_config.get("dev"), fake.aws)

    text = render_text(statuses)

    assert text.startswith("DATA STATUS")
    assert "dev / UserCorpusBucket" in text
    assert text.endswith("Manual data transfer may be required for EMPTY durable resources.")
    assert "legacy" not in text.lower()
    assert json.loads(render_json(statuses))[0]["environment"] == "dev"
