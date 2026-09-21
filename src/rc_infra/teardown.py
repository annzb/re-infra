"""Delete an environment that was removed from envs.yaml.

Order matters for safe retries. The rc-env-<env> stack is the record that an
environment still exists, so it is deleted last: if anything fails midway, the next
run sees the stack, recomputes the inventory, and continues where it stopped.

    rc-app-<env> stack -> schema-sync tables -> buckets (emptied) -> rc-env-<env> stack
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from rc_infra.aws import Aws, Stack
from rc_infra.env_config import (
    APP_STACK_PREFIX,
    PROTECTED_ENVIRONMENTS,
    RESOURCE_PREFIX,
    core_stack_name,
    table_prefix,
)
from rc_infra.tables import is_managed_by_environment
from rc_infra.templates import COMPONENT_TAG, ENVIRONMENT_TAG, MANAGED_BY_TAG, MANAGED_BY_VALUE


class TeardownRefused(Exception):
    """A safety check failed; nothing was deleted by the check that raised this."""


@dataclass(frozen=True)
class Inventory:
    environment: str
    core_stack: str
    app_stack: str
    app_stack_exists: bool
    tables: tuple[str, ...]
    unmanaged_tables: tuple[str, ...]
    buckets: tuple[str, ...]

    def describe(self) -> list[str]:
        lines = []
        if self.app_stack_exists:
            lines.append(f"delete app stack {self.app_stack}")
        lines.extend(f"delete table {name}" for name in self.tables)
        lines.extend(f"empty and delete bucket {name}" for name in self.buckets)
        lines.append(f"delete stack {self.core_stack}")
        lines.extend(
            f"skip table {name} (not tagged ManagedBy=rc-dynamo-sync, Environment={self.environment}; delete manually if intended)"
            for name in self.unmanaged_tables
        )
        return lines


def check_deletable(environment: str, stack: Stack, declared_names: Collection[str]) -> None:
    if environment in PROTECTED_ENVIRONMENTS:
        raise TeardownRefused(f"{environment} is protected and can never be torn down")
    if environment in declared_names:
        raise TeardownRefused(f"{environment} is still declared in envs.yaml")
    if stack.name != core_stack_name(environment):
        raise TeardownRefused(f"{stack.name} is not the core stack of {environment}")
    expected_tags = {
        MANAGED_BY_TAG: MANAGED_BY_VALUE,
        COMPONENT_TAG: "environment",
        ENVIRONMENT_TAG: environment,
    }
    mismatched = {k: stack.tags.get(k) for k, v in expected_tags.items() if stack.tags.get(k) != v}
    if mismatched:
        raise TeardownRefused(f"{stack.name} is not managed by re-infra (tags: {mismatched})")
    if stack.termination_protection:
        raise TeardownRefused(f"{stack.name} has termination protection enabled")


def inventory(environment: str, aws: Aws) -> Inventory:
    prefix = table_prefix(environment)
    tables: list[str] = []
    unmanaged: list[str] = []
    for name in aws.tables.list_names(prefix):
        if is_managed_by_environment(aws.tables.tags(name), environment):
            tables.append(name)
        else:
            unmanaged.append(name)

    core_stack = core_stack_name(environment)
    bucket_prefix = f"{RESOURCE_PREFIX}-{environment}-"
    buckets: list[str] = []
    for resource in aws.stacks.stack_resources(core_stack):
        if resource.resource_type != "AWS::S3::Bucket" or not resource.physical_id:
            continue
        if not resource.physical_id.startswith(bucket_prefix):
            raise TeardownRefused(f"{core_stack} bucket {resource.physical_id} does not start with {bucket_prefix}")
        buckets.append(resource.physical_id)

    app_stack = f"{APP_STACK_PREFIX}{environment}"
    return Inventory(
        environment=environment,
        core_stack=core_stack,
        app_stack=app_stack,
        app_stack_exists=aws.stacks.get_stack(app_stack) is not None,
        tables=tuple(tables),
        unmanaged_tables=tuple(unmanaged),
        buckets=tuple(sorted(buckets)),
    )


def teardown(
    environment: str,
    aws: Aws,
    declared_names: Collection[str],
    log: Callable[[str], None] = print,
) -> None:
    core_stack = core_stack_name(environment)
    stack = aws.stacks.get_stack(core_stack)
    if stack is None:
        log(f"{environment}: {core_stack} does not exist; nothing to tear down")
        return
    check_deletable(environment, stack, declared_names)

    found = inventory(environment, aws)
    if found.app_stack_exists:
        log(f"{environment}: deleting {found.app_stack}")
        aws.stacks.delete_stack(found.app_stack)
    for name in found.tables:
        # Re-read tags immediately before deleting; never trust an earlier listing.
        if not is_managed_by_environment(aws.tables.tags(name), environment):
            raise TeardownRefused(f"table {name} lost its ownership tags; refusing to delete")
        log(f"{environment}: deleting table {name}")
        aws.tables.delete(name)
    for name in found.buckets:
        log(f"{environment}: emptying and deleting bucket {name}")
        aws.buckets.empty_and_delete(name)
    for name in found.unmanaged_tables:
        log(f"{environment}: skipped unmanaged table {name}")

    log(f"{environment}: deleting {core_stack}")
    aws.stacks.delete_stack(core_stack)
