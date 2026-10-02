"""In-memory stand-ins for the boto3 DynamoDB and S3 clients rc-data-transfer uses.

Pages are deliberately tiny so every pagination path is exercised, and every write is
recorded so tests can assert that a refusal wrote nothing.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

from botocore.exceptions import ClientError

ACCOUNT = "273268178059"
PAGE_SIZE = 3


def table_arn(name: str) -> str:
    return f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/{name}"


def _not_found(operation: str, code: str = "ResourceNotFoundException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "not found"}}, operation)


def table_description(name: str, **overrides: Any) -> dict[str, Any]:
    description: dict[str, Any] = {
        "TableName": name,
        "TableArn": table_arn(name),
        "TableId": f"id-{name}",
        "TableStatus": "ACTIVE",
        "ItemCount": 0,
        "CreationDateTime": datetime(2026, 1, 1, tzinfo=UTC),
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
        "ProvisionedThroughput": {"ReadCapacityUnits": 0, "WriteCapacityUnits": 0, "NumberOfDecreasesToday": 0},
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "GSI1-Email",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
                "IndexStatus": "ACTIVE",
                "ItemCount": 0,
            }
        ],
    }
    description.update(overrides)
    return description


@dataclass
class FakeTable:
    description: dict[str, Any]
    ttl: dict[str, Any] = field(default_factory=lambda: {"TimeToLiveStatus": "ENABLED", "AttributeName": "expiresAt"})
    pitr: str = "ENABLED"
    tags: dict[str, str] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeDynamo:
    tables: dict[str, FakeTable] = field(default_factory=dict)
    writes: list[int] = field(default_factory=list)
    # How many batch_write_item calls answer with every request unprocessed.
    throttle_calls: int = 0

    def add(self, name: str, items: list[dict[str, Any]] | None = None, **overrides: Any) -> FakeTable:
        table = FakeTable(table_description(name, **overrides), items=list(items or []))
        self.tables[name] = table
        return table

    def _table(self, name: str, operation: str) -> FakeTable:
        if name not in self.tables:
            raise _not_found(operation)
        return self.tables[name]

    def describe_table(self, TableName: str) -> dict[str, Any]:
        return {"Table": copy.deepcopy(self._table(TableName, "DescribeTable").description)}

    def describe_time_to_live(self, TableName: str) -> dict[str, Any]:
        return {"TimeToLiveDescription": dict(self._table(TableName, "DescribeTimeToLive").ttl)}

    def describe_continuous_backups(self, TableName: str) -> dict[str, Any]:
        status = self._table(TableName, "DescribeContinuousBackups").pitr
        return {"ContinuousBackupsDescription": {"PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": status}}}

    def list_tags_of_resource(self, ResourceArn: str, NextToken: str | None = None) -> dict[str, Any]:
        name = ResourceArn.rsplit("/", 1)[1]
        return {"Tags": [{"Key": k, "Value": v} for k, v in self._table(name, "ListTagsOfResource").tags.items()]}

    def scan(
        self,
        TableName: str,
        Limit: int | None = None,
        Select: str | None = None,
        ConsistentRead: bool = False,
        ExclusiveStartKey: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        items = self._table(TableName, "Scan").items
        start = int(ExclusiveStartKey["offset"]["N"]) if ExclusiveStartKey else 0
        size = min(Limit or PAGE_SIZE, PAGE_SIZE)
        page = items[start : start + size]
        response: dict[str, Any] = {"Count": len(page)}
        if Select != "COUNT":
            response["Items"] = copy.deepcopy(page)
        if start + size < len(items):
            response["LastEvaluatedKey"] = {"offset": {"N": str(start + size)}}
        return response

    def batch_write_item(self, RequestItems: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        ((name, requests),) = RequestItems.items()
        assert len(requests) <= 25
        if self.throttle_calls:
            self.throttle_calls -= 1
            return {"UnprocessedItems": {name: requests}}
        table = self._table(name, "BatchWriteItem")
        table.items.extend(copy.deepcopy(request["PutRequest"]["Item"]) for request in requests)
        self.writes.append(len(requests))
        return {"UnprocessedItems": {}}


@dataclass
class FakeVersion:
    version_id: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    last_modified: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))


@dataclass
class FakeBucket:
    versioning: str | None = None
    # Key -> versions, oldest first. The last one is current.
    objects: dict[str, list[FakeVersion]] = field(default_factory=dict)
    delete_markers: int = 0


@dataclass
class FakeS3:
    buckets: dict[str, FakeBucket] = field(default_factory=dict)
    copies: list[tuple[str, str]] = field(default_factory=list)
    fail_copy: set[str] = field(default_factory=set)
    _counter: int = 0

    def put(self, bucket: str, key: str, body: bytes = b"x", **kwargs: Any) -> None:
        self._counter += 1
        versions = self.buckets[bucket].objects.setdefault(key, [])
        when = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=self._counter)
        versions.append(FakeVersion(f"v{self._counter}", body, last_modified=when, **kwargs))

    def _bucket(self, name: str) -> FakeBucket:
        if name not in self.buckets:
            raise _not_found("HeadBucket", "404")
        return self.buckets[name]

    def _version(self, bucket: str, key: str, version_id: str | None) -> FakeVersion:
        versions = self._bucket(bucket).objects.get(key)
        if not versions:
            raise _not_found("HeadObject", "404")
        if version_id is None:
            return versions[-1]
        return next(v for v in versions if v.version_id == version_id)

    def head_bucket(self, Bucket: str) -> dict[str, Any]:
        self._bucket(Bucket)
        return {}

    def get_bucket_versioning(self, Bucket: str) -> dict[str, Any]:
        status = self._bucket(Bucket).versioning
        return {"Status": status} if status else {}

    def list_objects_v2(self, Bucket: str, MaxKeys: int | None = None, ContinuationToken: str | None = None) -> dict[str, Any]:
        keys = sorted(self._bucket(Bucket).objects)
        start = int(ContinuationToken or 0)
        size = min(MaxKeys or 2, 2)
        page = keys[start : start + size]
        response: dict[str, Any] = {
            "KeyCount": len(page),
            "Contents": [{"Key": key, "Size": len(self._version(Bucket, key, None).body)} for key in page],
        }
        if start + size < len(keys):
            response["NextContinuationToken"] = str(start + size)
        return response

    def list_object_versions(self, Bucket: str, MaxKeys: int | None = None, **_: Any) -> dict[str, Any]:
        bucket = self._bucket(Bucket)
        versions = [
            {"Key": key, "VersionId": v.version_id, "Size": len(v.body), "LastModified": v.last_modified}
            for key, history in sorted(bucket.objects.items())
            for v in reversed(history)
        ]
        markers = [{"Key": "deleted", "VersionId": f"dm{i}"} for i in range(bucket.delete_markers)]
        if MaxKeys:
            versions, markers = versions[:MaxKeys], markers[: max(0, MaxKeys - len(versions))]
        return {"Versions": versions, "DeleteMarkers": markers, "IsTruncated": False}

    def head_object(self, Bucket: str, Key: str, VersionId: str | None = None) -> dict[str, Any]:
        version = self._version(Bucket, Key, VersionId)
        return {"ContentLength": len(version.body), "Metadata": dict(version.metadata), **version.headers}

    def get_object_tagging(self, Bucket: str, Key: str, VersionId: str | None = None) -> dict[str, Any]:
        return {"TagSet": [{"Key": k, "Value": v} for k, v in self._version(Bucket, Key, VersionId).tags.items()]}

    def copy(self, CopySource: dict[str, str], Bucket: str, Key: str, ExtraArgs: dict[str, Any]) -> None:
        if Key in self.fail_copy:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "CopyObject")
        assert ExtraArgs["MetadataDirective"] == "REPLACE" and ExtraArgs["TaggingDirective"] == "REPLACE"
        source = self._version(CopySource["Bucket"], CopySource["Key"], CopySource.get("VersionId"))
        headers = {k: v for k, v in ExtraArgs.items() if k not in {"Metadata", "MetadataDirective", "TaggingDirective", "Tagging"}}
        tags = dict(parse_qsl(ExtraArgs["Tagging"], keep_blank_values=True))
        self.put(Bucket, Key, source.body, headers=headers, metadata=dict(ExtraArgs["Metadata"]), tags=tags)
        self.copies.append((Key, CopySource.get("VersionId") or ""))


@dataclass
class FakeClients:
    dynamo: FakeDynamo = field(default_factory=FakeDynamo)
    s3_client: FakeS3 = field(default_factory=FakeS3)
    caller: str = ACCOUNT

    def account(self) -> str:
        return self.caller

    def dynamodb(self, region: str) -> Any:
        return self.dynamo

    def s3(self) -> Any:
        return self.s3_client
