"""In-memory stand-ins for the AWS protocols in rc_infra.aws.

Every mutating call is appended to a shared ``events`` list so tests can assert
ordering across stacks, tables, and buckets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
        parameters: Mapping[str, str],
        tags: Mapping[str, str],
        role_arn: str | None,
    ) -> list[ResourceChange]:
        return list(self.previews.get(stack_name, []))

    def deploy(
        self,
        stack_name: str,
        kind: ChangeSetKind,
        template_body: str,
        parameters: Mapping[str, str],
        tags: Mapping[str, str],
        role_arn: str | None,
        resources_to_import: Sequence[Mapping[str, Any]] = (),
    ) -> list[ResourceChange]:
        self.events.append(("deploy", kind.value, stack_name))
        self.deploys.append(
            {
                "stack": stack_name,
                "kind": kind,
                "template_body": template_body,
                "parameters": dict(parameters),
                "tags": dict(tags),
                "role_arn": role_arn,
                "resources_to_import": list(resources_to_import),
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
        return [ResourceChange("Add", "Something", "AWS::S3::Bucket")]

    def set_termination_protection(self, name: str, enabled: bool) -> None:
        self.events.append(("termination_protection", name, enabled))
        self.stacks[name] = replace(self.stacks[name], termination_protection=enabled)

    def delete_stack(self, name: str, role_arn: str | None) -> None:
        if name not in self.stacks:
            return
        self.events.append(("delete_stack", name))
        del self.stacks[name]
        self.resources.pop(name, None)


@dataclass
class FakeBuckets:
    events: list[tuple[Any, ...]]
    live: dict[str, dict[str, Any]] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    fail_delete_once: set[str] = field(default_factory=set)

    def exists(self, name: str) -> bool:
        return name in self.live

    def owner_stack(self, name: str) -> str | None:
        return self.owners.get(name)

    def live_config(self, name: str) -> dict[str, Any]:
        return self.live[name]

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

    def list_names(self, prefix: str) -> list[str]:
        return sorted(name for name in self.table_tags if name.startswith(prefix))

    def tags(self, name: str) -> dict[str, str]:
        return dict(self.table_tags[name])

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
        self.aws = Aws(
            stacks=self.stacks,
            buckets=self.buckets,
            tables=self.tables,
            cfn_role_arn="arn:aws:iam::273268178059:role/rc-infra-cfn-exec",
        )


def env_stack_tags(environment: str) -> dict[str, str]:
    return {"ManagedBy": "re-infra", "Component": "environment", "Environment": environment}


def dynamo_sync_tags(environment: str) -> dict[str, str]:
    return {
        "ManagedBy": "rc-dynamo-sync",
        "LifecycleOwner": "rc-dynamo-sync",
        "Environment": environment,
    }


def bucket_name(environment: str, purpose: str) -> str:
    return f"rc-{environment}-{purpose}-{ACCOUNT_ID}"


def matching_live_config(purpose: str) -> dict[str, Any]:
    """Live S3 responses that match infra/environment.yaml for a purpose."""
    lifecycle = {
        "user-corpus": {
            "Rules": [
                {
                    "ID": "ExpireRawUploads",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "uploads/"},
                    "Expiration": {"Days": 7},
                }
            ]
        },
        "schema-dumps": {
            "Rules": [
                {
                    "ID": "ExpireSchemaDumps",
                    "Status": "Enabled",
                    "Filter": {},
                    "Expiration": {"Days": 30},
                }
            ]
        },
    }.get(purpose)
    cors = {"CORSRules": [{"AllowedHeaders": ["*"], "AllowedMethods": ["PUT"], "AllowedOrigins": ["*"]}]} if purpose == "user-corpus" else None
    return {
        "encryption": {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                        "BucketKeyEnabled": False,
                    }
                ]
            }
        },
        "public_access_block": {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
        "versioning": {},
        "lifecycle": lifecycle,
        "cors": cors,
    }


def env_stack_resources(environment: str) -> list[StackResource]:
    from rc_infra.env_config import BUCKET_LOGICAL_IDS

    return [
        *(StackResource(logical_id, bucket_name(environment, purpose), "AWS::S3::Bucket") for purpose, logical_id in BUCKET_LOGICAL_IDS.items()),
        StackResource("RegionParameter", f"/rc/env/{environment}/region", "AWS::SSM::Parameter"),
    ]


def add_removed_environment(fake: FakeAws, name: str = "preview42") -> None:
    """An environment that exists in AWS but is no longer in the env_config."""
    from rc_infra.env_config import BUCKET_LOGICAL_IDS

    fake.stacks.add(f"rc-env-{name}", tags=env_stack_tags(name))
    fake.stacks.resources[f"rc-env-{name}"] = env_stack_resources(name)
    for purpose in BUCKET_LOGICAL_IDS:
        fake.buckets.live[bucket_name(name, purpose)] = {}
    fake.stacks.add(f"rc-app-{name}")
    fake.tables.table_tags[f"rc-{name}-users"] = dynamo_sync_tags(name)
    fake.tables.table_tags[f"rc-{name}-messages"] = dynamo_sync_tags(name)
    fake.tables.table_tags[f"rc-{name}-legacy"] = {}
