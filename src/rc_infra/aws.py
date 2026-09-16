"""Interfaces to the AWS operations rc-infra needs, and the value types they return.

Planning, applying, and teardown only talk to AWS through these protocols, so tests
exercise them against in-memory fakes (tests/fakes.py) instead of mocked boto3.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import boto3


class ChangeSetKind(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    IMPORT = "IMPORT"


@dataclass(frozen=True)
class Stack:
    name: str
    status: str
    tags: Mapping[str, str] = field(default_factory=dict)
    termination_protection: bool = False

    @property
    def is_pending_review(self) -> bool:
        """A stack shell left by a change set that was never executed; it has no resources."""
        return self.status == "REVIEW_IN_PROGRESS"

    @property
    def is_failed_create(self) -> bool:
        """A stack whose creation rolled back; it must be deleted before it can be recreated."""
        return self.status == "ROLLBACK_COMPLETE"

    @property
    def is_busy(self) -> bool:
        return self.status.endswith("_IN_PROGRESS") and not self.is_pending_review

    @property
    def is_broken(self) -> bool:
        return self.status.endswith("_FAILED")


@dataclass(frozen=True)
class ResourceChange:
    action: str
    logical_id: str
    resource_type: str
    replacement: str | None = None

    def describe(self) -> str:
        suffix = f" (replacement: {self.replacement})" if self.replacement else ""
        return f"{self.action} {self.logical_id} [{self.resource_type}]{suffix}"


@dataclass(frozen=True)
class StackResource:
    logical_id: str
    physical_id: str
    resource_type: str


class StackApi(Protocol):
    def get_stack(self, name: str) -> Stack | None: ...

    def list_stacks(self, prefix: str) -> list[Stack]: ...

    def stack_resources(self, name: str) -> list[StackResource]: ...

    def preview(
        self,
        stack_name: str,
        template_body: str,
        parameters: Mapping[str, str],
        tags: Mapping[str, str],
        role_arn: str | None,
    ) -> list[ResourceChange]:
        """Changes an UPDATE would make. Never executes anything."""
        ...

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
        """Create and execute a change set, waiting for completion. Returns what changed."""
        ...

    def set_termination_protection(self, name: str, enabled: bool) -> None: ...

    def delete_stack(self, name: str, role_arn: str | None) -> None:
        """Delete and wait. A stack that does not exist is already deleted."""
        ...


class BucketApi(Protocol):
    def exists(self, name: str) -> bool: ...

    def owner_stack(self, name: str) -> str | None:
        """The CloudFormation stack that manages the bucket, if any."""
        ...

    def live_config(self, name: str) -> dict[str, Any]:
        """Raw configuration responses, keyed as buckets.normalize_live expects."""
        ...

    def empty_and_delete(self, name: str) -> None:
        """Delete every object version and the bucket. A missing bucket is a no-op."""
        ...


class TableApi(Protocol):
    def list_names(self, prefix: str) -> list[str]: ...

    def tags(self, name: str) -> dict[str, str]: ...

    def delete(self, name: str) -> None:
        """Delete and wait. A missing table is a no-op."""
        ...


@dataclass(frozen=True)
class Aws:
    stacks: StackApi
    buckets: BucketApi
    tables: TableApi
    # Role CloudFormation assumes for every deploy/delete. None uses the caller's
    # credentials, which is only sensible for local experiments.
    cfn_role_arn: str | None = None


def connect(region: str, cfn_role_arn: str | None) -> Aws:
    from rc_infra.buckets import S3Buckets
    from rc_infra.cfn import CloudFormationStacks
    from rc_infra.tables import DynamoTables

    session = boto3.Session(region_name=region)
    return Aws(
        stacks=CloudFormationStacks(session.client("cloudformation")),
        buckets=S3Buckets(session.client("s3")),
        tables=DynamoTables(session.client("dynamodb")),
        cfn_role_arn=cfn_role_arn,
    )


def caller_account(region: str) -> str:
    identity = boto3.Session(region_name=region).client("sts").get_caller_identity()
    return str(identity["Account"])
