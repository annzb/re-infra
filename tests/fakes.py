"""In-memory stand-ins for the AWS protocols in rc_infra.aws.

Every mutating call is appended to a shared ``events`` list so tests can assert
ordering across stacks, tables, and buckets.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from rc_infra.aws import Aws, ChangeSetKind, ResourceChange, Stack, StackResource

ACCOUNT_ID = "273268178059"


@dataclass
class FakeStacks:
    events: list[tuple[Any, ...]]
    stacks: dict[str, Stack] = field(default_factory=dict)
    resources: dict[str, list[StackResource]] = field(default_factory=dict)
    previews: dict[str, list[ResourceChange]] = field(default_factory=dict)
    # What the change set apply executes contains, when it differs from the preview.
    executed: dict[str, list[ResourceChange]] = field(default_factory=dict)
    fail_deploy: set[str] = field(default_factory=set)
    deploys: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, *, tags: Mapping[str, str] | None = None, **kwargs: Any) -> Stack:
        stack = Stack(
            name=name,
            status=kwargs.pop("status", "CREATE_COMPLETE"),
            tags=dict(tags or {}),
            **kwargs,
        )
        self.stacks[name] = stack
        return stack

    def get_stack(self, name: str) -> Stack | None:
        return self.stacks.get(name)

    def list_stacks(self, prefix: str) -> list[Stack]:
        return sorted((s for s in self.stacks.values() if s.name.startswith(prefix)), key=lambda s: s.name)

    def stack_resources(self, name: str) -> list[StackResource]:
        return list(self.resources.get(name, []))

    def preview(
        self,
        stack_name: str,
        template_body: str,
        tags: Mapping[str, str],
        parameters: Mapping[str, str] | None = None,
    ) -> list[ResourceChange]:
        return list(self.previews.get(stack_name, []))

    def deploy(
        self,
        stack_name: str,
        kind: ChangeSetKind,
        template_body: str,
        tags: Mapping[str, str],
        guard: Callable[[list[ResourceChange]], None] | None = None,
        parameters: Mapping[str, str] | None = None,
    ) -> list[ResourceChange]:
        changes = self.executed.get(stack_name, [ResourceChange("Add", "Something", "AWS::S3::Bucket")])
        if guard is not None:
            guard(changes)
        self.events.append(("deploy", kind.value, stack_name))
        self.deploys.append(
            {
                "stack": stack_name,
                "kind": kind,
                "template_body": template_body,
                "tags": dict(tags),
                "parameters": dict(parameters or {}),
            }
        )
        if stack_name in self.fail_deploy:
            raise RuntimeError(f"simulated failure deploying {stack_name}")
        existing = self.stacks.get(stack_name)
        status = f"{kind.value}_COMPLETE"
        if existing is None:
            self.add(stack_name, tags=tags, status=status)
        else:
            self.stacks[stack_name] = replace(existing, status=status, tags=dict(tags))
        return list(changes)

    def set_termination_protection(self, name: str, enabled: bool) -> None:
        self.events.append(("termination_protection", name, enabled))
        self.stacks[name] = replace(self.stacks[name], termination_protection=enabled)

    def delete_stack(self, name: str) -> None:
        if name not in self.stacks:
            return
        self.events.append(("delete_stack", name))
        del self.stacks[name]
        self.resources.pop(name, None)


@dataclass
class FakeBuckets:
    events: list[tuple[Any, ...]]
    # Bucket name -> current object count. Versions holds noncurrent versions only.
    live: dict[str, int] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    fail_delete_once: set[str] = field(default_factory=set)

    def exists(self, name: str) -> bool:
        return name in self.live

    def is_empty(self, name: str, include_versions: bool = False) -> bool:
        return self.live[name] == 0 and not (include_versions and self.versions.get(name))

    def empty_and_delete(self, name: str) -> None:
        if name in self.fail_delete_once:
            self.fail_delete_once.discard(name)
            raise RuntimeError(f"simulated failure deleting {name}")
        if name not in self.live:
            return
        self.events.append(("delete_bucket", name))
        del self.live[name]


@dataclass
class FakeTables:
    events: list[tuple[Any, ...]]
    table_tags: dict[str, dict[str, str]] = field(default_factory=dict)
    items: dict[str, int] = field(default_factory=dict)

    def list_names(self, prefix: str) -> list[str]:
        return sorted(name for name in self.table_tags if name.startswith(prefix))

    def tags(self, name: str) -> dict[str, str]:
        return dict(self.table_tags[name])

    def is_empty(self, name: str) -> bool:
        return not self.items.get(name)

    def delete(self, name: str) -> None:
        if name not in self.table_tags:
            return
        self.events.append(("delete_table", name))
        del self.table_tags[name]


@dataclass
class FakeAws:
    events: list[tuple[Any, ...]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.stacks = FakeStacks(self.events)
        self.buckets = FakeBuckets(self.events)
        self.tables = FakeTables(self.events)
        self.aws = Aws(stacks=self.stacks, buckets=self.buckets, tables=self.tables)


def env_stack_tags(environment: str) -> dict[str, str]:
    return {"ManagedBy": "re-infra", "Component": "environment", "Environment": environment}


def dynamo_sync_tags(environment: str) -> dict[str, str]:
    return {
        "ManagedBy": "rc-dynamo-sync",
        "LifecycleOwner": "rc-dynamo-sync",
        "Environment": environment,
    }


def bucket_name(environment: str, purpose: str) -> str:
    """What CloudFormation generates for a bucket in rc-env-<environment>."""
    from rc_infra.env_config import BUCKET_LOGICAL_IDS

    return f"rc-env-{environment}-{BUCKET_LOGICAL_IDS[purpose].lower()}-a1b2c3d4e5f6"


def env_stack_outputs(environment: str) -> dict[str, str]:
    from rc_infra.env_config import BUCKET_LOGICAL_IDS

    outputs: dict[str, str] = {}
    for purpose, logical_id in BUCKET_LOGICAL_IDS.items():
        name = bucket_name(environment, purpose)
        outputs[f"{logical_id}Name"] = name
        outputs[f"{logical_id}Arn"] = f"arn:aws:s3:::{name}"
    return outputs


def env_stack_resources(environment: str) -> list[StackResource]:
    from rc_infra.env_config import BUCKET_LOGICAL_IDS

    return [StackResource(logical_id, bucket_name(environment, purpose), "AWS::S3::Bucket") for purpose, logical_id in BUCKET_LOGICAL_IDS.items()]


def add_removed_environment(fake: FakeAws, name: str = "preview42", *, core_leftovers: bool = False) -> None:
    """An environment that exists in AWS but is no longer in the env_config.

    With core_leftovers, retribalize-core has not yet deleted its app stack and tables.
    A table without core's ownership tags is always present: it is never core's, so it
    never blocks teardown and is never deleted.
    """
    from rc_infra.env_config import BUCKET_LOGICAL_IDS, table_prefix

    fake.stacks.add(f"rc-env-{name}", tags=env_stack_tags(name))
    fake.stacks.resources[f"rc-env-{name}"] = env_stack_resources(name)
    for purpose in BUCKET_LOGICAL_IDS:
        fake.buckets.live[bucket_name(name, purpose)] = 0
    fake.tables.table_tags[f"{table_prefix(name)}legacy"] = {}
    if core_leftovers:
        fake.stacks.add(f"rc-app-{name}")
        fake.tables.table_tags[f"{table_prefix(name)}users"] = dynamo_sync_tags(name)
        fake.tables.table_tags[f"{table_prefix(name)}messages"] = dynamo_sync_tags(name)
