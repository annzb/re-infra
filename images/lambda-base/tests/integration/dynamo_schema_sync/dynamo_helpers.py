"""Low-level boto3 helpers for the schema-sync integration tests.

These deliberately use direct boto3 calls (not the BaseTable models) because the
tests need to construct *old*/live schemas that do not always match any declared
model — e.g. a table whose primary key or GSI key schema differs from what the
migration script expects.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

import boto3
from botocore.exceptions import ClientError

# Local convergence is near-instant; keep polling tight.
POLL_SECONDS = float(os.environ.get("DYNAMO_SCHEMA_POLL_SECONDS", "1"))


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def _endpoint() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL")


def dynamo_client() -> Any:
    return boto3.client("dynamodb", region_name=_region(), endpoint_url=_endpoint())


def dynamo_resource() -> Any:
    return boto3.resource("dynamodb", region_name=_region(), endpoint_url=_endpoint())


def s3_client() -> Any:
    return boto3.client("s3", region_name=_region(), endpoint_url=_endpoint())


# ─────────────────────────── schema construction ───────────────────────────


def _key_schema_to_boto(key_schema: Mapping[str, Optional[str]]) -> List[Dict[str, str]]:
    boto = [{"AttributeName": key_schema["partition_key"], "KeyType": "HASH"}]
    if key_schema.get("sort_key"):
        boto.append({"AttributeName": key_schema["sort_key"], "KeyType": "RANGE"})
    return boto


def _collect_attribute_definitions(
    key_schema: Mapping[str, Optional[str]],
    gsis: Optional[Mapping[str, Mapping[str, Optional[str]]]],
    attribute_types: Optional[Mapping[str, str]],
) -> List[Dict[str, str]]:
    attrs: Dict[str, str] = {}

    def add(ks: Mapping[str, Optional[str]]) -> None:
        for role in ("partition_key", "sort_key"):
            name = ks.get(role)
            if name:
                attrs.setdefault(name, "S")

    add(key_schema)
    for gsi in (gsis or {}).values():
        add(gsi)
    if attribute_types:
        attrs.update(attribute_types)

    return [
        {"AttributeName": name, "AttributeType": attr_type}
        for name, attr_type in sorted(attrs.items())
    ]


def _projection_to_boto(gsi: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a Projection block. Defaults to ALL when the case does not care."""
    projection: Dict[str, Any] = {"ProjectionType": gsi.get("projection") or "ALL"}
    if projection["ProjectionType"] == "INCLUDE":
        projection["NonKeyAttributes"] = sorted(gsi["non_key_attributes"])
    return projection


def create_live_table(
    table_name: str,
    key_schema: Mapping[str, Optional[str]],
    gsis: Optional[Mapping[str, Mapping[str, Any]]] = None,
    attribute_types: Optional[Mapping[str, str]] = None,
) -> None:
    """Create a table with an arbitrary (possibly non-model) key/GSI schema."""
    client = dynamo_client()
    params: Dict[str, Any] = {
        "TableName": table_name,
        "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": _collect_attribute_definitions(key_schema, gsis, attribute_types),
        "KeySchema": _key_schema_to_boto(key_schema),
    }

    gsi_defs = [
        {
            "IndexName": name,
            "KeySchema": _key_schema_to_boto(gsi),
            "Projection": _projection_to_boto(gsi),
        }
        for name, gsi in (gsis or {}).items()
    ]
    if gsi_defs:
        params["GlobalSecondaryIndexes"] = gsi_defs

    client.create_table(**params)
    wait_table_active(table_name)


# ─────────────────────────── waiters / lifecycle ───────────────────────────


def wait_table_active(table_name: str) -> None:
    client = dynamo_client()
    while True:
        table = client.describe_table(TableName=table_name)["Table"]
        status = table.get("TableStatus")
        gsi_statuses = [g.get("IndexStatus") for g in table.get("GlobalSecondaryIndexes", [])]
        if status == "ACTIVE" and all(s == "ACTIVE" for s in gsi_statuses):
            return
        time.sleep(POLL_SECONDS)


def wait_table_deleted(table_name: str) -> None:
    client = dynamo_client()
    waiter = client.get_waiter("table_not_exists")
    waiter.wait(
        TableName=table_name,
        WaiterConfig={"Delay": max(1, int(POLL_SECONDS)), "MaxAttempts": 60},
    )


def table_exists(table_name: str) -> bool:
    client = dynamo_client()
    try:
        client.describe_table(TableName=table_name)
        return True
    except client.exceptions.ResourceNotFoundException:
        return False


def delete_table(table_name: str) -> None:
    client = dynamo_client()
    try:
        client.delete_table(TableName=table_name)
    except client.exceptions.ResourceNotFoundException:
        return
    wait_table_deleted(table_name)


def delete_tables_by_prefix(prefix: str) -> List[str]:
    client = dynamo_client()
    names: List[str] = []
    paginator = client.get_paginator("list_tables")
    for page in paginator.paginate():
        names.extend(page.get("TableNames", []))

    deleted: List[str] = []
    for name in names:
        if name.startswith(prefix):
            delete_table(name)
            deleted.append(name)
    return deleted


# ─────────────────────────── items ───────────────────────────


def _to_decimal(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    return value


def put_items(table_name: str, items: Sequence[Mapping[str, Any]]) -> None:
    table = dynamo_resource().Table(table_name)
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=_to_decimal(dict(item)))


def scan_items(table_name: str) -> List[Dict[str, Any]]:
    table = dynamo_resource().Table(table_name)
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


# ─────────────────────────── introspection ───────────────────────────


def _parse_key_schema(boto_key_schema: Sequence[Mapping[str, str]]) -> Dict[str, Optional[str]]:
    partition_key: Optional[str] = None
    sort_key: Optional[str] = None
    for element in boto_key_schema:
        if element["KeyType"] == "HASH":
            partition_key = element["AttributeName"]
        elif element["KeyType"] == "RANGE":
            sort_key = element["AttributeName"]
    return {"partition_key": partition_key, "sort_key": sort_key}


def describe_schema(table_name: str) -> Dict[str, Any]:
    """Return {key_schema, gsis} normalized like BaseTable.actual_schema()."""
    client = dynamo_client()
    table = client.describe_table(TableName=table_name)["Table"]
    return {
        "key_schema": _parse_key_schema(table.get("KeySchema", [])),
        "gsis": {
            gsi["IndexName"]: _parse_key_schema(gsi.get("KeySchema", []))
            for gsi in table.get("GlobalSecondaryIndexes", [])
        },
    }


def describe_projections(table_name: str) -> Dict[str, Any]:
    """Live projection per index, as (type, sorted non-key attributes or None)."""
    client = dynamo_client()
    table = client.describe_table(TableName=table_name)["Table"]
    return {
        gsi["IndexName"]: (
            gsi["Projection"].get("ProjectionType"),
            tuple(sorted(gsi["Projection"].get("NonKeyAttributes", []))) or None,
        )
        for gsi in table.get("GlobalSecondaryIndexes", [])
    }


# ─────────────────────────── S3 dumps ───────────────────────────


def ensure_bucket(bucket: str) -> None:
    client = s3_client()
    try:
        client.head_bucket(Bucket=bucket)
        return
    except ClientError:
        pass

    params: Dict[str, Any] = {"Bucket": bucket}
    region = _region()
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}
    client.create_bucket(**params)


def bucket_exists(bucket: str) -> bool:
    client = s3_client()
    try:
        client.head_bucket(Bucket=bucket)
        return True
    except ClientError:
        return False


def list_dump_objects(bucket: str, table_name: Optional[str] = None) -> List[str]:
    client = s3_client()
    prefix = f"dynamo-schema-sync/{table_name}/" if table_name else "dynamo-schema-sync/"
    keys: List[str] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
    except client.exceptions.NoSuchBucket:
        return []
    return keys


def delete_bucket_objects(bucket: str, prefix: str = "dynamo-schema-sync/") -> None:
    client = s3_client()
    try:
        paginator = client.get_paginator("list_objects_v2")
        to_delete: List[Dict[str, str]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            to_delete.extend({"Key": obj["Key"]} for obj in page.get("Contents", []))
    except client.exceptions.NoSuchBucket:
        return

    for start in range(0, len(to_delete), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": to_delete[start : start + 1000]})


def delete_bucket(bucket: str) -> None:
    client = s3_client()
    try:
        delete_bucket_objects(bucket, prefix="")
        client.delete_bucket(Bucket=bucket)
    except ClientError:
        pass


# ─────────────────────────── tags ───────────────────────────


def list_tags(table_name: str) -> Dict[str, str]:
    client = dynamo_client()
    arn = client.describe_table(TableName=table_name)["Table"]["TableArn"]
    tags: Dict[str, str] = {}
    kwargs: Dict[str, Any] = {"ResourceArn": arn}
    while True:
        response = client.list_tags_of_resource(**kwargs)
        tags.update({tag["Key"]: tag["Value"] for tag in response.get("Tags", [])})
        token = response.get("NextToken")
        if not token:
            return tags
        kwargs["NextToken"] = token
