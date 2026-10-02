"""Report which durable resources of each environment hold data. Read-only.

Fresh resources start empty; this is how an operator sees where a manual data
transfer (rc-data-transfer) may be needed. It deliberately says nothing about where
that data would come from: that mapping belongs to the transfer tool alone.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum

from rc_infra.aws import Aws
from rc_infra.env_config import BUCKET_LOGICAL_IDS, Environment
from rc_infra.tables import is_managed_by_environment
from rc_infra.templates import ENVIRONMENT_TEMPLATE_PATH, bucket_output_keys, load_template


class State(StrEnum):
    EMPTY = "EMPTY"
    NON_EMPTY = "NON-EMPTY"
    NOT_CREATED = "NOT CREATED"


@dataclass(frozen=True)
class ResourceStatus:
    environment: str
    kind: str  # "bucket" or "table"
    # The bucket's logical ID, or the table name without the environment prefix.
    resource: str
    physical_name: str | None
    state: State


def environment_status(env: Environment, aws: Aws) -> list[ResourceStatus]:
    return [*_bucket_status(env, aws), *_table_status(env, aws)]


def _bucket_status(env: Environment, aws: Aws) -> list[ResourceStatus]:
    stack = aws.stacks.get_stack(env.core_stack)
    outputs = stack.outputs if stack is not None else {}
    versioned = _versioned_buckets()
    statuses = []
    for logical_id in BUCKET_LOGICAL_IDS.values():
        name = outputs.get(bucket_output_keys(logical_id)[0])
        if name is None:
            state = State.NOT_CREATED
        else:
            # A versioned bucket holding only old versions or delete markers is not pristine.
            state = State.EMPTY if aws.buckets.is_empty(name, include_versions=logical_id in versioned) else State.NON_EMPTY
        statuses.append(ResourceStatus(env.name, "bucket", logical_id, name, state))
    return statuses


def _table_status(env: Environment, aws: Aws) -> list[ResourceStatus]:
    names = [name for name in aws.tables.list_names(env.table_prefix) if is_managed_by_environment(aws.tables.tags(name), env.name)]
    if not names:
        # Tables are created by retribalize-core's schema sync, not by apply.
        return [ResourceStatus(env.name, "table", "(tables)", None, State.NOT_CREATED)]
    return [
        ResourceStatus(
            env.name,
            "table",
            name.removeprefix(env.table_prefix),
            name,
            State.EMPTY if aws.tables.is_empty(name) else State.NON_EMPTY,
        )
        for name in names
    ]


def _versioned_buckets() -> frozenset[str]:
    resources = load_template(ENVIRONMENT_TEMPLATE_PATH)["Resources"]
    return frozenset(
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Properties", {}).get("VersioningConfiguration", {}).get("Status") == "Enabled"
    )


def render_text(statuses: Iterable[ResourceStatus]) -> str:
    rows = list(statuses)
    lines = ["DATA STATUS"]
    width = max((len(f"{s.environment} / {s.resource}") for s in rows), default=0)
    for status in rows:
        label = f"{status.environment} / {status.resource}"
        lines.append(f"  {label:<{width}}  {status.state.value}")
    if any(s.state is State.EMPTY for s in rows):
        lines.append("Manual data transfer may be required for EMPTY durable resources.")
    return "\n".join(lines)


def render_json(statuses: Iterable[ResourceStatus]) -> str:
    return json.dumps([asdict(s) for s in statuses], indent=2)
