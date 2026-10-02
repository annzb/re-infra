"""Copy every item of one DynamoDB table into an empty table with exactly the same configuration.

Items travel as the low-level client's raw AttributeValues, so nothing is converted
on the way: numbers keep their precision, sets stay sets, binary stays binary.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from rc_infra.transfer.models import Counts, Diff

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

BATCH_SIZE = 25  # BatchWriteItem's limit
MAX_ATTEMPTS = 8
_BACKOFF_SECONDS = 0.1
_BACKOFF_CAP_SECONDS = 5.0


class CopyFailed(Exception):
    pass


def canonical_config(client: DynamoDBClient, name: str) -> dict[str, Any]:
    """The configuration two tables must share for a copy to be exact. Volatile fields are left out."""
    table: Mapping[str, Any] = client.describe_table(TableName=name)["Table"]
    ttl: Mapping[str, Any] = client.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
    billing = table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
    provisioned = billing == "PROVISIONED"
    stream = table.get("StreamSpecification", {})
    sse = table.get("SSEDescription") or {}
    return {
        "key_schema": _key_schema(table["KeySchema"]),
        "attribute_definitions": {a["AttributeName"]: a["AttributeType"] for a in table.get("AttributeDefinitions", [])},
        "billing_mode": billing,
        "provisioned_throughput": _throughput(table.get("ProvisionedThroughput")) if provisioned else None,
        "local_secondary_indexes": {index["IndexName"]: _index(index, provisioned=False) for index in table.get("LocalSecondaryIndexes", [])},
        "global_secondary_indexes": {index["IndexName"]: _index(index, provisioned=provisioned) for index in table.get("GlobalSecondaryIndexes", [])},
        "stream": {"enabled": bool(stream.get("StreamEnabled")), "view_type": stream.get("StreamViewType")},
        # An AWS-owned key reports no SSEDescription at all.
        "sse": {"type": sse.get("SSEType", "AWS_OWNED"), "kms_key": sse.get("KMSMasterKeyArn")},
        "table_class": table.get("TableClassSummary", {}).get("TableClass", "STANDARD"),
        "ttl": {"enabled": ttl.get("TimeToLiveStatus") in {"ENABLED", "ENABLING"}, "attribute": ttl.get("AttributeName")},
    }


def advisory_config(client: DynamoDBClient, name: str) -> dict[str, Any]:
    """Reported for review, but a difference here does not block the copy."""
    arn = client.describe_table(TableName=name)["Table"]["TableArn"]
    backups = client.describe_continuous_backups(TableName=name)["ContinuousBackupsDescription"]
    tags: dict[str, str] = {}
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"ResourceArn": arn}
        if next_token:
            kwargs["NextToken"] = next_token
        page = client.list_tags_of_resource(**kwargs)
        tags.update({tag["Key"]: tag["Value"] for tag in page.get("Tags", [])})
        next_token = page.get("NextToken")
        if not next_token:
            break
    return {
        "point_in_time_recovery": backups.get("PointInTimeRecoveryDescription", {}).get("PointInTimeRecoveryStatus"),
        "tags": tags,
    }


def diff(source: Any, target: Any, path: str = "", blocking: bool = True) -> list[Diff]:
    """Field-level differences between two canonical configurations, in a stable order."""
    if isinstance(source, dict) and isinstance(target, dict):
        found: list[Diff] = []
        for key in sorted(set(source) | set(target)):
            found.extend(diff(source.get(key), target.get(key), f"{path}.{key}" if path else str(key), blocking))
        return found
    return [] if source == target else [Diff(path, source, target, blocking)]


def is_empty(client: DynamoDBClient, name: str) -> bool:
    return not client.scan(TableName=name, Limit=1, ConsistentRead=True).get("Items")


def count(client: DynamoDBClient, name: str) -> int:
    """Exact item count by a consistent scan. DescribeTable's ItemCount is approximate and hours stale."""
    total = 0
    start_key: Mapping[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {"TableName": name, "Select": "COUNT", "ConsistentRead": True}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = client.scan(**kwargs)
        total += page["Count"]
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return total


def copy_items(
    source_client: DynamoDBClient,
    source: str,
    target_client: DynamoDBClient,
    target: str,
    counts: Counts,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Scan the source and put every item into the target. Updates counts as it goes, so a
    caller interrupted midway still knows how far it got. Raises CopyFailed if a batch keeps
    coming back unprocessed; nothing already written is undone."""
    start_key: Mapping[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {"TableName": source, "ConsistentRead": True}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = source_client.scan(**kwargs)
        items = page.get("Items", [])
        counts.scanned += len(items)
        for offset in range(0, len(items), BATCH_SIZE):
            _write_batch(target_client, target, items[offset : offset + BATCH_SIZE], counts, sleep)
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return


def _write_batch(client: DynamoDBClient, table: str, items: list[Any], counts: Counts, sleep: Callable[[float], None]) -> None:
    requests: list[Any] = [{"PutRequest": {"Item": item}} for item in items]
    for attempt in range(MAX_ATTEMPTS):
        response = client.batch_write_item(RequestItems={table: requests})
        unprocessed = response.get("UnprocessedItems", {}).get(table, [])
        counts.written += len(requests) - len(unprocessed)
        if not unprocessed:
            return
        counts.retries += 1
        requests = list(unprocessed)
        sleep(min(_BACKOFF_SECONDS * 2**attempt, _BACKOFF_CAP_SECONDS))
    counts.failed += len(requests)
    raise CopyFailed(f"{len(requests)} item(s) still unprocessed after {MAX_ATTEMPTS} attempts")


def _key_schema(keys: list[Mapping[str, Any]]) -> dict[str, str]:
    return {key["KeyType"]: key["AttributeName"] for key in keys}


def _throughput(throughput: Mapping[str, Any] | None) -> dict[str, int] | None:
    if not throughput:
        return None
    return {"read": throughput.get("ReadCapacityUnits", 0), "write": throughput.get("WriteCapacityUnits", 0)}


def _index(index: Mapping[str, Any], *, provisioned: bool) -> dict[str, Any]:
    projection = index.get("Projection", {})
    return {
        "key_schema": _key_schema(index["KeySchema"]),
        "projection": projection.get("ProjectionType"),
        "non_key_attributes": sorted(projection.get("NonKeyAttributes", [])),
        "provisioned_throughput": _throughput(index.get("ProvisionedThroughput")) if provisioned else None,
    }
