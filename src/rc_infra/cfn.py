"""CloudFormation operations: change sets, waiting, and failure reporting."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError, WaiterError

from rc_infra.aws import ChangeSetKind, ResourceChange, Stack, StackResource

if TYPE_CHECKING:
    from mypy_boto3_cloudformation import CloudFormationClient

_NO_CHANGES_REASONS = ("didn't contain changes", "No updates are to be performed")
_WAITERS = {
    ChangeSetKind.CREATE: "stack_create_complete",
    ChangeSetKind.UPDATE: "stack_update_complete",
    ChangeSetKind.IMPORT: "stack_import_complete",
}
# Stack operations on these templates take minutes; allow up to an hour.
_WAITER_CONFIG: Any = {"Delay": 10, "MaxAttempts": 360}
# rc-platform declares the shared Lambda execution role by name, so every change
# set must acknowledge named IAM resources or CloudFormation refuses to create it.
_CAPABILITIES = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"]


class DeployError(Exception):
    pass


class CloudFormationStacks:
    def __init__(self, client: CloudFormationClient) -> None:
        self._cfn = client

    def get_stack(self, name: str) -> Stack | None:
        try:
            stacks = self._cfn.describe_stacks(StackName=name)["Stacks"]
        except ClientError as exc:
            if "does not exist" in exc.response["Error"].get("Message", ""):
                return None
            raise
        return _stack(stacks[0]) if stacks else None

    def list_stacks(self, prefix: str) -> list[Stack]:
        stacks: list[Stack] = []
        for page in self._cfn.get_paginator("describe_stacks").paginate():
            stacks.extend(_stack(s) for s in page["Stacks"] if s["StackName"].startswith(prefix))
        return sorted(stacks, key=lambda stack: stack.name)

    def stack_resources(self, name: str) -> list[StackResource]:
        resources: list[StackResource] = []
        for page in self._cfn.get_paginator("list_stack_resources").paginate(StackName=name):
            resources.extend(
                StackResource(
                    logical_id=r["LogicalResourceId"],
                    physical_id=r.get("PhysicalResourceId", ""),
                    resource_type=r["ResourceType"],
                )
                for r in page["StackResourceSummaries"]
            )
        return resources

    def preview(
        self,
        stack_name: str,
        template_body: str,
        parameters: Mapping[str, str],
        tags: Mapping[str, str],
    ) -> list[ResourceChange]:
        change_set_id, changes = self._create_change_set(stack_name, ChangeSetKind.UPDATE, template_body, parameters, tags, ())
        if change_set_id:
            self._cfn.delete_change_set(ChangeSetName=change_set_id)
        return changes

    def deploy(
        self,
        stack_name: str,
        kind: ChangeSetKind,
        template_body: str,
        parameters: Mapping[str, str],
        tags: Mapping[str, str],
        resources_to_import: Sequence[Mapping[str, Any]] = (),
    ) -> list[ResourceChange]:
        change_set_id, changes = self._create_change_set(stack_name, kind, template_body, parameters, tags, resources_to_import)
        if not change_set_id:
            return []
        self._cfn.execute_change_set(ChangeSetName=change_set_id)
        try:
            self._cfn.get_waiter(_WAITERS[kind]).wait(  # type: ignore[call-overload]
                StackName=stack_name, WaiterConfig=_WAITER_CONFIG
            )
        except WaiterError as exc:
            raise DeployError(f"{stack_name}: {kind} failed\n{self._failure_events(stack_name)}") from exc
        return changes

    def set_termination_protection(self, name: str, enabled: bool) -> None:
        self._cfn.update_termination_protection(StackName=name, EnableTerminationProtection=enabled)

    def delete_stack(self, name: str) -> None:
        if self.get_stack(name) is None:
            return
        self._cfn.delete_stack(StackName=name)
        try:
            self._cfn.get_waiter("stack_delete_complete").wait(StackName=name, WaiterConfig=_WAITER_CONFIG)
        except WaiterError as exc:
            raise DeployError(f"{name}: delete failed\n{self._failure_events(name)}") from exc

    def _create_change_set(
        self,
        stack_name: str,
        kind: ChangeSetKind,
        template_body: str,
        parameters: Mapping[str, str],
        tags: Mapping[str, str],
        resources_to_import: Sequence[Mapping[str, Any]],
    ) -> tuple[str | None, list[ResourceChange]]:
        kwargs: dict[str, Any] = {
            "StackName": stack_name,
            "ChangeSetName": f"rc-infra-{uuid.uuid4().hex[:12]}",
            "ChangeSetType": kind.value,
            "TemplateBody": template_body,
            "Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()],
            "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
            "Capabilities": _CAPABILITIES,
        }
        if resources_to_import:
            kwargs["ResourcesToImport"] = list(resources_to_import)

        change_set_id = self._cfn.create_change_set(**kwargs)["Id"]
        try:
            self._cfn.get_waiter("change_set_create_complete").wait(ChangeSetName=change_set_id, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
        except WaiterError as exc:
            described = self._cfn.describe_change_set(ChangeSetName=change_set_id)
            reason = described.get("StatusReason", "")
            if any(marker in reason for marker in _NO_CHANGES_REASONS):
                self._cfn.delete_change_set(ChangeSetName=change_set_id)
                return None, []
            raise DeployError(f"{stack_name}: change set failed: {reason}") from exc

        changes: list[ResourceChange] = []
        next_token: str | None = None
        while True:
            page_kwargs: dict[str, Any] = {"ChangeSetName": change_set_id}
            if next_token:
                page_kwargs["NextToken"] = next_token
            described = self._cfn.describe_change_set(**page_kwargs)
            for change in described.get("Changes", []):
                resource = change.get("ResourceChange", {})
                changes.append(
                    ResourceChange(
                        action=resource.get("Action", "?"),
                        logical_id=resource.get("LogicalResourceId", "?"),
                        resource_type=resource.get("ResourceType", "?"),
                        replacement=resource.get("Replacement") or None,
                    )
                )
            next_token = described.get("NextToken")
            if not next_token:
                break
        return change_set_id, changes

    def _failure_events(self, stack_name: str, limit: int = 10) -> str:
        try:
            events = self._cfn.describe_stack_events(StackName=stack_name)["StackEvents"]
        except ClientError:
            return "(stack events unavailable)"
        failures = [e for e in events if e.get("ResourceStatus", "").endswith("_FAILED")][:limit]
        return (
            "\n".join(f"  {e['LogicalResourceId']}: {e.get('ResourceStatusReason', e['ResourceStatus'])}" for e in failures)
            or "  (no failed resource events)"
        )


def _stack(raw: Mapping[str, Any]) -> Stack:
    return Stack(
        name=raw["StackName"],
        status=raw["StackStatus"],
        tags={tag["Key"]: tag["Value"] for tag in raw.get("Tags", [])},
        termination_protection=bool(raw.get("EnableTerminationProtection", False)),
    )
