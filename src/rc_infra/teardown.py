"""Delete an environment that was removed from envs.yaml.

re-infra only ever deletes what it created: the environment's buckets and its
rc-env-<env> stack. The application stack (rc-app-<env>) and the environment's
tables belong to retribalize-core, which must remove them first. While either still
exists, teardown is refused, so a bucket the application still uses is never emptied.

Order matters for safe retries. The rc-env-<env> stack is the record that an
environment still exists, so it is deleted last: if anything fails midway, the next
run sees the stack, recomputes the inventory, and continues where it stopped.

    buckets (emptied) -> rc-env-<env> stack
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from rc_infra.aws import Aws, Stack
from rc_infra.env_config import (
    APP_STACK_PREFIX,
    PROTECTED_ENVIRONMENTS,
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
    buckets: tuple[str, ...]

    def describe(self) -> list[str]:
        lines = [f"empty and delete bucket {name}" for name in self.buckets]
        lines.append(f"delete stack {self.core_stack}")
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
    """What teardown would delete. Raises TeardownRefused while core still has resources here."""
    _check_core_cleaned_up(environment, aws)

    core_stack = core_stack_name(environment)
    # CloudFormation names the stack's buckets <stack>-<logical id>-<suffix>.
    bucket_prefix = f"{core_stack}-"
    buckets: list[str] = []
    for resource in aws.stacks.stack_resources(core_stack):
        if resource.resource_type != "AWS::S3::Bucket" or not resource.physical_id:
            continue
        if not resource.physical_id.startswith(bucket_prefix):
            raise TeardownRefused(f"{core_stack} bucket {resource.physical_id} does not start with {bucket_prefix}")
        buckets.append(resource.physical_id)

    return Inventory(environment=environment, core_stack=core_stack, buckets=tuple(sorted(buckets)))


def _check_core_cleaned_up(environment: str, aws: Aws) -> None:
    app_stack = f"{APP_STACK_PREFIX}{environment}"
    if aws.stacks.get_stack(app_stack) is not None:
        raise TeardownRefused(f"{app_stack} still exists; retribalize-core must delete it before the buckets can go")
    tables = [name for name in aws.tables.list_names(table_prefix(environment)) if is_managed_by_environment(aws.tables.tags(name), environment)]
    if tables:
        raise TeardownRefused(f"retribalize-core must delete the tables of {environment} first: {', '.join(tables)}")


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
    for name in found.buckets:
        log(f"{environment}: emptying and deleting bucket {name}")
        aws.buckets.empty_and_delete(name)

    log(f"{environment}: deleting {core_stack}")
    aws.stacks.delete_stack(core_stack)
