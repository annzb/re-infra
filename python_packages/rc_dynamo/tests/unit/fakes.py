"""A small in-memory DynamoDB table fake for the OO table wrappers.

``BaseTable`` talks to its boto3 ``Table`` handle with:
- ``put_item(Item=, ConditionExpression=<str>, ExpressionAttributeNames=)`` where
  the condition is ``attribute_exists(#pk)`` / ``attribute_not_exists(#pk)``;
- ``get_item(Key=)`` / ``delete_item(Key=)``;
- ``query(KeyConditionExpression=<boto3 cond>, [IndexName=], [FilterExpression=])``
  and ``scan(FilterExpression=<boto3 cond>)`` with boto3 ``Key``/``Attr``
  condition *objects* (not strings).

This fake evaluates those condition objects via ``.get_expression()`` so tests
exercise the real serialization/query-planning code without moto or AWS
credentials (matching the repo's fake-table convention).
"""

from __future__ import annotations

import re
from typing import Any

from boto3.dynamodb.conditions import AttributeBase, ConditionBase
from botocore.exceptions import ClientError


def _attr_name(value: Any) -> Any:
    return value.name if isinstance(value, AttributeBase) else value


def _matches(item: dict[str, Any], condition: ConditionBase | None) -> bool:
    """Evaluate a boto3 Key/Attr ConditionBase against a stored item."""
    if condition is None:
        return True
    expr = condition.get_expression()
    operator = expr["operator"]
    values = expr["values"]

    if operator == "AND":
        return all(_matches(item, value) for value in values)
    if operator == "OR":
        return any(_matches(item, value) for value in values)
    if operator == "NOT":
        return not _matches(item, values[0])

    name = _attr_name(values[0])
    actual = item.get(name)
    if operator == "=":
        return actual == values[1]
    if operator == "begins_with":
        return isinstance(actual, str) and actual.startswith(values[1])
    if operator == "attribute_exists":
        return name in item
    if operator == "attribute_not_exists":
        return name not in item
    raise NotImplementedError(f"FakeDynamoTable does not support operator {operator!r}")


class ConditionalCheckFailed(ClientError):
    def __init__(self) -> None:
        super().__init__(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "failed"}},
            "PutItem",
        )


class FakeDynamoTable:
    """In-memory stand-in for a boto3 ``Table`` handle."""

    def __init__(self, key_fields: tuple[str, str | None], gsis=None):
        # key_fields: (hash_attr, range_attr|None) for the base table.
        self.name = "FakeTable"
        self.key_fields = key_fields
        self.gsis = gsis or {}
        self.items: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    # ── key helpers ──
    def _key_of(self, item: dict[str, Any]) -> tuple[Any, Any]:
        hk, rk = self.key_fields
        return item.get(hk), (item.get(rk) if rk else None)

    def _find(self, key: dict[str, Any]) -> dict[str, Any] | None:
        hk, rk = self.key_fields
        for it in self.items:
            if it.get(hk) == key.get(hk) and (rk is None or it.get(rk) == key.get(rk)):
                return it
        return None

    # ── writes ──
    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None, **kwargs):
        self.put_calls.append(Item)
        existing = self._find(Item)
        if ConditionExpression:
            names = ExpressionAttributeNames or {}
            expr = ConditionExpression
            for placeholder, real in names.items():
                expr = expr.replace(placeholder, real)
            if expr.lstrip("(").startswith("attribute_not_exists(") and existing is not None:
                raise ConditionalCheckFailed()
            if re.search(r"attribute_exists\(", expr) and existing is None:
                raise ConditionalCheckFailed()
        if existing is not None:
            self.items.remove(existing)
        self.items.append(dict(Item))

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ReturnValues="ALL_NEW",
        **kwargs,
    ):
        call = {
            "Key": Key,
            "UpdateExpression": UpdateExpression,
            "ExpressionAttributeNames": ExpressionAttributeNames or {},
            "ExpressionAttributeValues": ExpressionAttributeValues or {},
            "ConditionExpression": ConditionExpression,
            "ReturnValues": ReturnValues,
            **kwargs,
        }
        self.update_calls.append(call)

        existing = self._find(Key)
        if ConditionExpression:
            names = ExpressionAttributeNames or {}
            expr = ConditionExpression
            if isinstance(expr, str):
                for placeholder, real in names.items():
                    expr = expr.replace(placeholder, real)
                if re.search(r"attribute_exists\(", expr) and existing is None:
                    raise ConditionalCheckFailed()
                if expr.lstrip("(").startswith("attribute_not_exists(") and existing is not None:
                    raise ConditionalCheckFailed()

        if existing is None:
            existing = dict(Key)
            self.items.append(existing)

        if not UpdateExpression.startswith("SET "):
            raise NotImplementedError("FakeDynamoTable only supports SET update expressions")
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        for assignment in UpdateExpression[4:].split(","):
            left, right = assignment.strip().split(" = ", 1)
            field_name = names.get(left, left)
            existing[field_name] = values[right]

        return {"Attributes": dict(existing)} if ReturnValues != "NONE" else {}

    def delete_item(self, Key, **kwargs):
        existing = self._find(Key)
        if existing is not None:
            self.items.remove(existing)

    # ── reads ──
    def _project(self, item: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
        projection = kwargs.get("ProjectionExpression")
        if not projection:
            return dict(item)

        names = kwargs.get("ExpressionAttributeNames") or {}
        projected: dict[str, Any] = {}
        for part in projection.split(","):
            token = part.strip()
            if not token:
                continue
            field_name = names.get(token, token)
            if field_name in item:
                projected[field_name] = item[field_name]
        return projected

    def get_item(self, Key, **kwargs):
        found = self._find(Key)
        return {"Item": self._project(found, kwargs)} if found is not None else {}

    def query(self, **kwargs):
        key_condition = kwargs.get("KeyConditionExpression")
        filter_expression = kwargs.get("FilterExpression")
        results = [
            self._project(it, kwargs)
            for it in self.items
            if _matches(it, key_condition) and _matches(it, filter_expression)
        ]
        return {"Items": results}

    def scan(self, **kwargs):
        filter_expression = kwargs.get("FilterExpression")
        results = [
            self._project(it, kwargs) for it in self.items if _matches(it, filter_expression)
        ]
        return {"Items": results}


# ───────────────────────── control plane ─────────────────────────


def client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeWaiter:
    def __init__(self, client: FakeDynamoClient) -> None:
        self.client = client

    def wait(self, TableName, WaiterConfig=None):
        assert self.client.description is None, "table still exists"


class FakeDynamoClient:
    """In-memory stand-in for the DynamoDB *client* calls the schema tooling makes.

    Holds one table's control-plane state -- DescribeTable fields, TTL, PITR,
    Kinesis destinations, resource policy -- and applies CreateTable /
    UpdateTable / UpdateTimeToLive / UpdateContinuousBackups to it, so a sync run
    converges and its post-apply re-diff sees the result. Every mutating call is
    recorded in ``calls`` as ``(operation, params)``.

    ``description=None`` means the table does not exist.
    """

    def __init__(
        self,
        description: dict[str, Any] | None = None,
        *,
        ttl_attribute: str | None = None,
        pitr: bool = False,
        kinesis_destinations: list[dict[str, Any]] | None = None,
        resource_policy: str | None = None,
        pitr_unavailable_times: int = 0,
    ) -> None:
        self.description = description
        self.ttl_attribute = ttl_attribute
        self.pitr = pitr
        self.kinesis_destinations = kinesis_destinations or []
        self.resource_policy = resource_policy
        self.pitr_unavailable_times = pitr_unavailable_times
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def operations(self) -> list[str]:
        return [operation for operation, _ in self.calls]

    def params_of(self, operation: str) -> list[dict[str, Any]]:
        return [params for op, params in self.calls if op == operation]

    # ── reads ──
    def describe_table(self, TableName):
        if self.description is None:
            raise client_error("ResourceNotFoundException", "DescribeTable")
        return {
            "Table": {
                "TableName": TableName,
                "TableStatus": "ACTIVE",
                "TableArn": f"arn:aws:dynamodb:us-east-1:000000000000:table/{TableName}",
                **self.description,
            }
        }

    def describe_time_to_live(self, TableName):
        if self.ttl_attribute is None:
            return {"TimeToLiveDescription": {"TimeToLiveStatus": "DISABLED"}}
        return {
            "TimeToLiveDescription": {
                "TimeToLiveStatus": "ENABLED",
                "AttributeName": self.ttl_attribute,
            }
        }

    def describe_continuous_backups(self, TableName):
        status = "ENABLED" if self.pitr else "DISABLED"
        return {
            "ContinuousBackupsDescription": {
                "ContinuousBackupsStatus": "ENABLED",
                "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": status},
            }
        }

    def describe_kinesis_streaming_destination(self, TableName):
        return {"TableName": TableName, "KinesisDataStreamDestinations": self.kinesis_destinations}

    def get_resource_policy(self, ResourceArn):
        if self.resource_policy is None:
            raise client_error("PolicyNotFoundException", "GetResourcePolicy")
        return {"Policy": self.resource_policy, "RevisionId": "1"}

    def get_waiter(self, name):
        assert name == "table_not_exists"
        return _FakeWaiter(self)

    # ── writes ──
    def create_table(self, **params):
        self.calls.append(("create_table", params))
        description: dict[str, Any] = {
            "KeySchema": params["KeySchema"],
            "AttributeDefinitions": params["AttributeDefinitions"],
            "GlobalSecondaryIndexes": [
                {**gsi, "IndexStatus": "ACTIVE"} for gsi in params.get("GlobalSecondaryIndexes", [])
            ],
            "BillingModeSummary": {"BillingMode": params.get("BillingMode", "PROVISIONED")},
            "DeletionProtectionEnabled": params.get("DeletionProtectionEnabled", False),
        }
        if "TableClass" in params:
            description["TableClassSummary"] = {"TableClass": params["TableClass"]}
        if "StreamSpecification" in params:
            description["StreamSpecification"] = params["StreamSpecification"]
        self.description = description

    def update_table(self, TableName, **params):
        self.calls.append(("update_table", params))
        assert self.description is not None
        if "BillingMode" in params:
            self.description["BillingModeSummary"] = {"BillingMode": params["BillingMode"]}
        if "TableClass" in params:
            self.description["TableClassSummary"] = {"TableClass": params["TableClass"]}
        if "DeletionProtectionEnabled" in params:
            self.description["DeletionProtectionEnabled"] = params["DeletionProtectionEnabled"]
        if "StreamSpecification" in params:
            spec = params["StreamSpecification"]
            if spec["StreamEnabled"]:
                assert not self.description.get("StreamSpecification", {}).get("StreamEnabled"), (
                    "DynamoDB rejects enabling a stream on a table that already has one"
                )
                self.description["StreamSpecification"] = spec
            else:
                self.description.pop("StreamSpecification", None)

    def update_time_to_live(self, TableName, TimeToLiveSpecification):
        self.calls.append(("update_time_to_live", TimeToLiveSpecification))
        if TimeToLiveSpecification["Enabled"]:
            assert self.ttl_attribute is None, "DynamoDB rejects enabling TTL twice"
            self.ttl_attribute = TimeToLiveSpecification["AttributeName"]
        else:
            self.ttl_attribute = None

    def update_continuous_backups(self, TableName, PointInTimeRecoverySpecification):
        if self.pitr_unavailable_times:
            self.pitr_unavailable_times -= 1
            raise client_error("ContinuousBackupsUnavailableException", "UpdateContinuousBackups")
        self.calls.append(("update_continuous_backups", PointInTimeRecoverySpecification))
        self.pitr = PointInTimeRecoverySpecification["PointInTimeRecoveryEnabled"]

    def delete_table(self, TableName):
        self.calls.append(("delete_table", {"TableName": TableName}))
        assert not (self.description or {}).get("DeletionProtectionEnabled"), "protected table"
        # TTL and PITR belong to the physical table: a recreated one starts without.
        self.description, self.ttl_attribute, self.pitr = None, None, False


def live_description(
    *,
    partition_key: str = "pk",
    billing_mode: str = "PAY_PER_REQUEST",
    **extra: Any,
) -> dict[str, Any]:
    """A DescribeTable ``Table`` body for a pk-only table, plus any extra fields."""
    return {
        "KeySchema": [{"AttributeName": partition_key, "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": partition_key, "AttributeType": "S"}],
        "BillingModeSummary": {"BillingMode": billing_mode},
        **extra,
    }
