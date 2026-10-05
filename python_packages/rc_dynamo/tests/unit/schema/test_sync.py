"""Schema-sync without DynamoDB: tags, dump-bucket guard, create params, table options."""

from types import SimpleNamespace
from typing import ClassVar

import pytest
from botocore.exceptions import ClientError

from rc_dynamo import BaseItem, BaseTable, TableOptions
from rc_dynamo.schema import sync
from rc_dynamo.schema.sync import (
    SchemaSyncError,
    create_table,
    ensure_table_tags,
    managed_tags,
    require_dump_bucket,
    sync_tables,
)
from rc_dynamo.utils.settings import Settings

from ..fakes import FakeDynamoClient, client_error, live_description

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


def test_create_table_without_options_sends_exactly_what_it_always_did():
    # Backward compatibility: no table_options -> no option parameters, no follow-ups.
    client = _CreateClient()
    create_table(_table_on(client), settings=FAST)
    assert set(client.created) == {
        "TableName",
        "BillingMode",
        "AttributeDefinitions",
        "KeySchema",
    }


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


# ───────────────────────── table options ─────────────────────────

FAST_GRANTS = Settings(schema_poll_seconds=0.01, schema_wait_timeout_seconds=1)


def _with_options(client, **options) -> BaseTable:
    class _Optioned(BaseTable[_Item]):
        table_name = "rc-preview3-things"
        item_model = _Item
        table_options = TableOptions(**options)

    return _Optioned(SimpleNamespace(meta=SimpleNamespace(client=client)))


SESSIONS = {
    "ttl_attribute": "expiresAt",
    "point_in_time_recovery": True,
    "deletion_protection": True,
    "table_class": "STANDARD",
    "stream_view_type": "NEW_IMAGE",
}


def test_create_applies_declared_options():
    client = FakeDynamoClient(None)

    create_table(_with_options(client, **SESSIONS), settings=FAST)

    created = client.params_of("create_table")[0]
    assert created["BillingMode"] == "PAY_PER_REQUEST"
    assert created["DeletionProtectionEnabled"] is True
    assert created["TableClass"] == "STANDARD"
    assert created["StreamSpecification"] == {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"}
    # TTL and PITR have no CreateTable parameter: follow-up calls once ACTIVE.
    assert client.operations() == [
        "create_table",
        "update_time_to_live",
        "update_continuous_backups",
    ]
    assert client.params_of("update_time_to_live") == [
        {"Enabled": True, "AttributeName": "expiresAt"}
    ]
    assert client.params_of("update_continuous_backups") == [{"PointInTimeRecoveryEnabled": True}]


def test_create_with_explicitly_disabled_options_makes_no_follow_up_calls():
    client = FakeDynamoClient(None)
    create_table(
        _with_options(client, ttl_attribute=None, point_in_time_recovery=False), settings=FAST
    )
    assert client.operations() == ["create_table"]


def test_create_retries_pitr_while_backups_are_initialising():
    client = FakeDynamoClient(None, pitr_unavailable_times=2)
    create_table(_with_options(client, point_in_time_recovery=True), settings=FAST)
    assert client.pitr is True


def test_create_gives_up_on_pitr_after_the_wait_timeout():
    client = FakeDynamoClient(None, pitr_unavailable_times=10**6)
    settings = Settings(schema_poll_seconds=0.01, schema_wait_timeout_seconds=0.05)
    with pytest.raises(ClientError, match="ContinuousBackupsUnavailable"):
        create_table(_with_options(client, point_in_time_recovery=True), settings=settings)


def test_create_refuses_provisioned_billing_before_calling_aws():
    client = FakeDynamoClient(None)
    with pytest.raises(SchemaSyncError, match="PROVISIONED"):
        create_table(_with_options(client, billing_mode="PROVISIONED"), settings=FAST)
    assert client.operations() == []


def test_sync_creates_a_missing_table_with_its_options_and_converges():
    client = FakeDynamoClient(None)
    assert sync_tables([_with_options(client, **SESSIONS)], apply=True, settings=FAST) is True
    assert client.ttl_attribute == "expiresAt"
    assert client.pitr is True


def test_sync_applies_declared_option_drift_in_place():
    client = FakeDynamoClient(
        live_description(
            billing_mode="PROVISIONED",
            TableClassSummary={"TableClass": "STANDARD_INFREQUENT_ACCESS"},
        )
    )
    table = _with_options(client, billing_mode="PAY_PER_REQUEST", **SESSIONS)

    # Raises if anything is left unresolved after --apply.
    assert sync_tables([table], apply=True, settings=FAST) is True

    assert client.operations() == [
        "update_table",  # billing first: before anything else touches the table
        "update_table",
        "update_table",
        "update_continuous_backups",
        "update_time_to_live",
        "update_table",
    ]
    assert client.params_of("update_table") == [
        {"BillingMode": "PAY_PER_REQUEST"},
        {"TableClass": "STANDARD"},
        {"DeletionProtectionEnabled": True},
        {"StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"}},
    ]
    assert "delete_table" not in client.operations()


def test_sync_dry_run_reports_option_drift_without_changing_anything(capsys):
    client = FakeDynamoClient(live_description())
    table = _with_options(client, ttl_attribute="expiresAt")

    assert sync_tables([table], apply=False, settings=FAST) is True
    assert client.operations() == []
    assert "[update_options]" in capsys.readouterr().out


def test_sync_never_turns_off_unmanaged_options():
    client = FakeDynamoClient(
        live_description(DeletionProtectionEnabled=True), ttl_attribute="expiresAt", pitr=True
    )
    # Nothing declared: everything live is inherited, nothing is pending.
    assert sync_tables([_table_on(client)], apply=True, settings=FAST) is False
    assert client.operations() == []
    assert (client.ttl_attribute, client.pitr) == ("expiresAt", True)


def test_sync_moves_ttl_to_another_attribute_by_disabling_first():
    client = FakeDynamoClient(live_description(), ttl_attribute="ttl")
    sync_tables([_with_options(client, ttl_attribute="expiresAt")], apply=True, settings=FAST)
    assert client.params_of("update_time_to_live") == [
        {"Enabled": False, "AttributeName": "ttl"},
        {"Enabled": True, "AttributeName": "expiresAt"},
    ]


def test_ttl_move_rate_limited_by_dynamodb_explains_the_second_deploy():
    client = FakeDynamoClient(live_description(), ttl_attribute="ttl")
    original = client.update_time_to_live

    def rate_limited(TableName, TimeToLiveSpecification):
        if TimeToLiveSpecification["Enabled"]:
            raise client_error("ValidationException", "UpdateTimeToLive")
        original(TableName=TableName, TimeToLiveSpecification=TimeToLiveSpecification)

    client.update_time_to_live = rate_limited  # type: ignore[method-assign]
    with pytest.raises(SchemaSyncError, match="one TTL change per hour"):
        sync_tables([_with_options(client, ttl_attribute="expiresAt")], apply=True, settings=FAST)


def test_sync_changes_a_stream_view_type_by_disabling_first():
    client = FakeDynamoClient(
        live_description(StreamSpecification={"StreamEnabled": True, "StreamViewType": "KEYS_ONLY"})
    )
    sync_tables([_with_options(client, stream_view_type="NEW_IMAGE")], apply=True, settings=FAST)
    assert client.params_of("update_table") == [
        {"StreamSpecification": {"StreamEnabled": False}},
        {"StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"}},
    ]


@pytest.mark.parametrize("option", ["point_in_time_recovery", "deletion_protection"])
def test_disabling_a_protection_is_refused_without_permission(option):
    client = FakeDynamoClient(live_description(DeletionProtectionEnabled=True), pitr=True)
    protections = {"point_in_time_recovery": True, "deletion_protection": True}
    table = _with_options(client, **{**protections, option: False})

    with pytest.raises(SchemaSyncError, match="DYNAMO_ALLOW_PROTECTION_DOWNGRADE"):
        sync_tables([table], apply=True, settings=FAST)
    assert client.operations() == []

    allowed = Settings(
        schema_poll_seconds=0.01, schema_wait_timeout_seconds=1, allow_protection_downgrade=True
    )
    assert sync_tables([table], apply=True, settings=allowed) is True
    assert table.actual_schema()["options"][option] is False


def test_switching_to_provisioned_fails_the_run_even_on_a_dry_run():
    client = FakeDynamoClient(live_description())
    with pytest.raises(SchemaSyncError, match="no permission flag"):
        sync_tables([_with_options(client, billing_mode="PROVISIONED")], apply=False, settings=FAST)


# ───────────────────────── recreate guards ─────────────────────────

RECREATE = Settings(
    schema_poll_seconds=0.01, schema_wait_timeout_seconds=1, allow_table_recreate=True
)

# The declaration's key is "pk"; a live key of "old_pk" forces a recreate.
OLD_KEY = {
    "KeySchema": [{"AttributeName": "old_pk", "KeyType": "HASH"}],
    "AttributeDefinitions": [{"AttributeName": "old_pk", "AttributeType": "S"}],
}

LSI_DESCRIPTION = [
    {
        "IndexName": "by-created",
        "KeySchema": [
            {"AttributeName": "old_pk", "KeyType": "HASH"},
            {"AttributeName": "created", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }
]


@pytest.fixture
def no_dumps(monkeypatch):
    """Fail the test if anything reaches the dump/restore stage."""

    def touched(*args, **kwargs):
        raise AssertionError("the table was dumped")

    monkeypatch.setattr(sync, "require_dump_bucket", lambda bucket, settings=None: bucket)
    monkeypatch.setattr(sync, "_dump_table_to_s3", touched)


@pytest.mark.parametrize(
    "extra,reason",
    [
        ({"LocalSecondaryIndexes": LSI_DESCRIPTION}, "local secondary index 'by-created'"),
        (
            {"StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"}},
            "undeclared stream",
        ),
        (
            {"SSEDescription": {"Status": "ENABLED", "SSEType": "KMS", "KMSMasterKeyArn": "k"}},
            "KMS encryption",
        ),
        ({"DeletionProtectionEnabled": True}, "deletion protection enabled"),
    ],
)
def test_recreate_is_refused_with_unreproducible_live_features(no_dumps, extra, reason):
    client = FakeDynamoClient(live_description(**{**OLD_KEY, **extra}))

    with pytest.raises(SchemaSyncError, match=reason):
        sync_tables([_table_on(client)], apply=True, settings=RECREATE, dump_bucket="dumps")
    assert client.operations() == []


@pytest.mark.parametrize(
    "client_kwargs,reason",
    [
        (
            {"kinesis_destinations": [{"StreamArn": "arn:kinesis", "DestinationStatus": "ACTIVE"}]},
            "Kinesis streaming destination",
        ),
        ({"resource_policy": "{}"}, "resource policy"),
    ],
)
def test_recreate_preflight_refuses_attachments_describe_table_cannot_see(
    no_dumps, client_kwargs, reason
):
    client = FakeDynamoClient(live_description(**OLD_KEY), **client_kwargs)

    with pytest.raises(SchemaSyncError, match=reason):
        sync_tables([_table_on(client)], apply=True, settings=RECREATE, dump_bucket="dumps")
    assert client.operations() == []


def test_recreate_preflight_fails_closed_when_it_cannot_check(no_dumps):
    client = FakeDynamoClient(live_description(**OLD_KEY))

    def denied(TableName):
        raise client_error("AccessDeniedException", "DescribeKinesisStreamingDestination")

    client.describe_kinesis_streaming_destination = denied  # type: ignore[method-assign]
    with pytest.raises(SchemaSyncError, match="could not be checked: AccessDeniedException"):
        sync_tables([_table_on(client)], apply=True, settings=RECREATE, dump_bucket="dumps")


def test_recreate_carries_declared_and_inherited_options_to_the_new_table(monkeypatch):
    monkeypatch.setattr(sync, "require_dump_bucket", lambda bucket, settings=None: bucket)
    monkeypatch.setattr(
        sync, "_dump_table_to_s3", lambda table, bucket, settings: {"bucket": bucket, "key": "k"}
    )
    monkeypatch.setattr(sync, "_restore_table_from_s3", lambda table, ref, settings: None)
    monkeypatch.setattr(sync, "_delete_dump", lambda ref, settings: None)

    # Unmanaged TTL and PITR on the old table; declared deletion protection.
    client = FakeDynamoClient(live_description(**OLD_KEY), ttl_attribute="expiresAt", pitr=True)
    table = _with_options(client, deletion_protection=True)

    assert sync_tables([table], apply=True, settings=RECREATE, dump_bucket="dumps") is True

    created = client.params_of("create_table")[0]
    assert created["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
    assert created["DeletionProtectionEnabled"] is True
    assert client.operations() == [
        "delete_table",
        "create_table",
        "update_time_to_live",
        "update_continuous_backups",
    ]
    assert (client.ttl_attribute, client.pitr) == ("expiresAt", True)
