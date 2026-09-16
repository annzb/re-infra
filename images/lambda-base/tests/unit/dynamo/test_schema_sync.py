"""Schema-sync helpers that need no DynamoDB: tags, dump-bucket guard, create params."""

from types import SimpleNamespace
from typing import ClassVar

import pytest
from botocore.exceptions import ClientError

from rc_lambda_base.dynamo import BaseItem, BaseTable
from rc_lambda_base.dynamo.schema_sync import (
    SchemaSyncError,
    create_table,
    ensure_table_tags,
    managed_tags,
    require_dump_bucket,
    sync_tables,
)
from rc_lambda_base.settings import Settings

FAST = Settings(schema_poll_seconds=0.01, schema_wait_timeout_seconds=1)


class _Item(BaseItem):
    partition_key: ClassVar[str] = "pk"
    pk: str
    note: str | None = None


class _Table(BaseTable[_Item]):
    table_name = "rc-preview3-things"
    item_model = _Item


def _not_found(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}}, operation
    )


def _table_on(client) -> _Table:
    return _Table(SimpleNamespace(meta=SimpleNamespace(client=client)))


class _TagClient:
    """describe_table + paginated list_tags_of_resource (one tag per page) + tag_resource."""

    def __init__(self, live_tags, exists=True):
        self.live = dict(live_tags)
        self.exists = exists
        self.tag_calls = []

    def describe_table(self, TableName):
        if not self.exists:
            raise _not_found("DescribeTable")
        return {
            "Table": {
                "TableName": TableName,
                "TableArn": f"arn:aws:dynamodb:us-east-1:000000000000:table/{TableName}",
            }
        }

    def list_tags_of_resource(self, ResourceArn, NextToken=None):
        items = sorted(self.live.items())
        start = int(NextToken or 0)
        response = {"Tags": [{"Key": k, "Value": v} for k, v in items[start : start + 1]]}
        if start + 1 < len(items):
            response["NextToken"] = str(start + 1)
        return response

    def tag_resource(self, ResourceArn, Tags):
        self.tag_calls.append((ResourceArn, Tags))


# ───────────────────────── managed_tags ─────────────────────────


def test_managed_tags_include_ownership_and_extras():
    assert managed_tags("preview3", {"Repository": "retribalize-core"}) == {
        "ManagedBy": "rc-dynamo-sync",
        "LifecycleOwner": "rc-dynamo-sync",
        "Environment": "preview3",
        "Repository": "retribalize-core",
    }


def test_managed_tags_require_an_environment():
    with pytest.raises(SchemaSyncError, match="environment"):
        managed_tags("")


def test_managed_tags_refuse_to_override_ownership_keys():
    with pytest.raises(SchemaSyncError, match="Environment"):
        managed_tags("preview3", {"Environment": "prod"})


# ───────────────────────── ensure_table_tags ─────────────────────────


def test_apply_adds_only_missing_or_different_tags_across_pages():
    client = _TagClient({"ManagedBy": "rc-dynamo-sync", "Environment": "old", "Team": "growth"})
    wanted = managed_tags("preview3")

    changes = ensure_table_tags(_table_on(client), wanted, apply=True)

    assert changes == {"LifecycleOwner": "rc-dynamo-sync", "Environment": "preview3"}
    assert client.tag_calls == [
        (
            "arn:aws:dynamodb:us-east-1:000000000000:table/rc-preview3-things",
            [
                {"Key": "Environment", "Value": "preview3"},
                {"Key": "LifecycleOwner", "Value": "rc-dynamo-sync"},
            ],
        )
    ]


def test_dry_run_reports_missing_tags_without_writing(capsys):
    client = _TagClient({})

    changes = ensure_table_tags(_table_on(client), managed_tags("preview3"), apply=False)

    assert set(changes) == {"ManagedBy", "LifecycleOwner", "Environment"}
    assert client.tag_calls == []
    assert "Would tag rc-preview3-things" in capsys.readouterr().out


def test_fully_tagged_table_is_left_alone():
    wanted = managed_tags("preview3")
    client = _TagClient(wanted)
    assert ensure_table_tags(_table_on(client), wanted, apply=True) == {}
    assert client.tag_calls == []


def test_missing_table_has_nothing_to_tag():
    client = _TagClient({}, exists=False)
    assert ensure_table_tags(_table_on(client), managed_tags("preview3"), apply=True) == {}


# ───────────────────────── create_table ─────────────────────────


class _CreateClient:
    def __init__(self):
        self.created = None

    def describe_table(self, TableName):
        if self.created is None:
            raise _not_found("DescribeTable")
        return {
            "Table": {"TableName": TableName, "TableStatus": "ACTIVE", "GlobalSecondaryIndexes": []}
        }

    def create_table(self, **params):
        self.created = params


def test_create_table_writes_tags_with_the_table():
    client = _CreateClient()

    create_table(
        _table_on(client), settings=FAST, tags=managed_tags("preview3", {"Repository": "core"})
    )

    assert client.created["TableName"] == "rc-preview3-things"
    assert client.created["BillingMode"] == "PAY_PER_REQUEST"
    assert client.created["Tags"] == [
        {"Key": "Environment", "Value": "preview3"},
        {"Key": "LifecycleOwner", "Value": "rc-dynamo-sync"},
        {"Key": "ManagedBy", "Value": "rc-dynamo-sync"},
        {"Key": "Repository", "Value": "core"},
    ]


def test_create_table_without_tags_sends_no_tags():
    client = _CreateClient()
    create_table(_table_on(client), settings=FAST)
    assert "Tags" not in client.created


# ───────────────────────── dump bucket ─────────────────────────


@pytest.mark.parametrize("bucket", [None, ""])
def test_dump_bucket_is_required(bucket):
    with pytest.raises(SchemaSyncError, match="required"):
        require_dump_bucket(bucket, settings=FAST)


def test_allowed_recreate_without_dump_bucket_fails_before_touching_tables():
    class _Untouchable:
        @property
        def table(self):
            raise AssertionError("a table was touched")

    with pytest.raises(SchemaSyncError, match="required"):
        sync_tables(
            [_Untouchable()],  # type: ignore[list-item]
            apply=True,
            settings=Settings(allow_table_recreate=True),
            dump_bucket="",
        )
