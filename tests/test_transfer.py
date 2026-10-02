from __future__ import annotations

import json
from typing import Any

import pytest

from rc_infra.transfer import dynamodb
from rc_infra.transfer.arns import ArnError, parse_bucket_arn, parse_table_arn
from rc_infra.transfer.cli import main
from rc_infra.transfer.jobs import transfer_bucket, transfer_table
from rc_infra.transfer.models import Outcome
from tests.transfer_fakes import FakeBucket, FakeClients, table_arn

SOURCE, TARGET = parse_table_arn(table_arn("old-users")), parse_table_arn(table_arn("new-users"))

# Every DynamoDB value type, exactly as the low-level client returns it.
MIXED_ITEMS = [
    {
        "pk": {"S": f"user#{n}"},
        "sk": {"S": "profile"},
        "amount": {"N": "12345678901234567890.123456789"},
        "blob": {"B": b"\x00\xff"},
        "nested": {"M": {"list": {"L": [{"S": "a"}, {"N": "1"}, {"NULL": True}]}, "flag": {"BOOL": False}}},
        "tags": {"SS": ["x", "y"]},
        "scores": {"NS": ["1", "2.5"]},
        "blobs": {"BS": [b"a", b"b"]},
    }
    for n in range(60)
]


def _no_sleep(_: float) -> None:
    pass


@pytest.fixture
def clients() -> FakeClients:
    fake = FakeClients()
    fake.dynamo.add("old-users", MIXED_ITEMS)
    fake.dynamo.add("new-users")
    return fake


# ── ARNs ────────────────────────────────────────────────────────────


def test_table_arn_is_parsed() -> None:
    arn = parse_table_arn("arn:aws:dynamodb:eu-west-2:123456789012:table/rc-dev-users")
    assert (arn.region, arn.account, arn.name) == ("eu-west-2", "123456789012", "rc-dev-users")


@pytest.mark.parametrize(
    "value",
    [
        "arn:aws:dynamodb:us-east-1:123456789012:table/users/index/GSI1",
        "arn:aws:s3:::bucket",
        "rc-dev-users",
        "arn:aws:dynamodb:us-east-1:1234:table/users",
    ],
)
def test_bad_table_arns_are_rejected(value: str) -> None:
    with pytest.raises(ArnError):
        parse_table_arn(value)


@pytest.mark.parametrize("value", ["arn:aws:s3:::bucket/key", "arn:aws:dynamodb:us-east-1:123456789012:table/t", "bucket", "arn:aws:s3:::Bad_Name"])
def test_bad_bucket_arns_are_rejected(value: str) -> None:
    with pytest.raises(ArnError):
        parse_bucket_arn(value)


# ── DynamoDB ────────────────────────────────────────────────────────


def test_dry_run_checks_and_counts_but_writes_nothing(clients: FakeClients) -> None:
    report = transfer_table(clients, SOURCE, TARGET, apply=False)

    assert report.outcome is Outcome.CHECKED
    assert report.counts.source_total == 60
    assert clients.dynamo.writes == []


def test_copy_preserves_every_value_exactly(clients: FakeClients) -> None:
    report = transfer_table(clients, SOURCE, TARGET, apply=True)

    assert report.outcome is Outcome.COPIED, report.to_text()
    assert clients.dynamo.tables["new-users"].items == MIXED_ITEMS
    assert (report.counts.scanned, report.counts.written, report.counts.target_total) == (60, 60, 60)
    assert max(clients.dynamo.writes) <= dynamodb.BATCH_SIZE


def test_unprocessed_items_are_retried(clients: FakeClients) -> None:
    clients.dynamo.throttle_calls = 2

    report = transfer_table(clients, SOURCE, TARGET, apply=True, sleep=_no_sleep)

    assert report.outcome is Outcome.COPIED
    assert report.counts.retries == 2
    assert len(clients.dynamo.tables["new-users"].items) == 60


def test_items_that_never_go_through_fail_the_copy(clients: FakeClients) -> None:
    clients.dynamo.throttle_calls = dynamodb.MAX_ATTEMPTS

    report = transfer_table(clients, SOURCE, TARGET, apply=True, sleep=_no_sleep)

    assert report.outcome is Outcome.FAILED
    assert report.counts.failed == 3  # the first page


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}]}, "key_schema.HASH"),
        ({"KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}]}, "key_schema.RANGE"),
        ({"GlobalSecondaryIndexes": []}, "global_secondary_indexes.GSI1-Email"),
        (
            {
                "GlobalSecondaryIndexes": [
                    {
                        "IndexName": "GSI1-Email",
                        "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["name"]},
                    }
                ]
            },
            "global_secondary_indexes.GSI1-Email.projection",
        ),
        ({"BillingModeSummary": {"BillingMode": "PROVISIONED"}}, "billing_mode"),
        ({"StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"}}, "stream.enabled"),
        ({"TableClassSummary": {"TableClass": "STANDARD_INFREQUENT_ACCESS"}}, "table_class"),
    ],
)
def test_any_configuration_difference_refuses_before_writing(clients: FakeClients, change: dict[str, Any], path: str) -> None:
    clients.dynamo.add("new-users", **change)

    report = transfer_table(clients, SOURCE, TARGET, apply=True)

    assert report.outcome is Outcome.REFUSED
    assert path in {d.path for d in report.diffs if d.blocking}
    assert clients.dynamo.writes == []
    assert "No data was written." in report.to_text()


def test_ttl_difference_refuses(clients: FakeClients) -> None:
    clients.dynamo.tables["new-users"].ttl = {"TimeToLiveStatus": "DISABLED"}

    report = transfer_table(clients, SOURCE, TARGET, apply=True)

    assert report.outcome is Outcome.REFUSED
    assert {d.path for d in report.diffs if d.blocking} == {"ttl.attribute", "ttl.enabled"}
    assert 'ttl.attribute:\n  source: "expiresAt"\n  target: null' in report.to_text()


def test_volatile_fields_and_tags_do_not_block(clients: FakeClients) -> None:
    clients.dynamo.tables["new-users"].description["ItemCount"] = 999
    clients.dynamo.tables["new-users"].tags = {"Environment": "dev"}
    clients.dynamo.tables["new-users"].pitr = "DISABLED"

    report = transfer_table(clients, SOURCE, TARGET, apply=False)

    assert report.outcome is Outcome.CHECKED
    assert {d.path for d in report.diffs} == {"tags.Environment", "point_in_time_recovery"}
    assert not any(d.blocking for d in report.diffs)


def test_non_empty_target_refuses(clients: FakeClients) -> None:
    clients.dynamo.tables["new-users"].items.append({"pk": {"S": "x"}, "sk": {"S": "y"}})

    report = transfer_table(clients, SOURCE, TARGET, apply=True)

    assert report.outcome is Outcome.REFUSED
    assert "not empty" in report.reasons[0]
    assert clients.dynamo.writes == []


def test_same_table_and_foreign_account_refuse(clients: FakeClients) -> None:
    assert transfer_table(clients, SOURCE, SOURCE, apply=True).outcome is Outcome.REFUSED
    foreign = parse_table_arn("arn:aws:dynamodb:us-east-1:111111111111:table/new-users")
    report = transfer_table(clients, SOURCE, foreign, apply=True)
    assert report.outcome is Outcome.REFUSED
    assert "cross-account" in report.reasons[0]


def test_missing_table_refuses(clients: FakeClients) -> None:
    missing = parse_table_arn(table_arn("nope"))
    assert transfer_table(clients, SOURCE, missing, apply=True).outcome is Outcome.REFUSED


def test_interrupt_keeps_what_was_written(clients: FakeClients, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = clients.dynamo.batch_write_item

    def interrupt_on_third(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return real(**kwargs)

    monkeypatch.setattr(clients.dynamo, "batch_write_item", interrupt_on_third)

    report = transfer_table(clients, SOURCE, TARGET, apply=True)

    assert report.outcome is Outcome.INTERRUPTED
    assert report.counts.written == 6
    assert len(clients.dynamo.tables["new-users"].items) == 6


# ── S3 ──────────────────────────────────────────────────────────────

OLD, NEW = parse_bucket_arn("arn:aws:s3:::old-bucket"), parse_bucket_arn("arn:aws:s3:::new-bucket")


@pytest.fixture
def buckets(clients: FakeClients) -> FakeClients:
    s3 = clients.s3_client
    s3.buckets["old-bucket"] = FakeBucket()
    s3.buckets["new-bucket"] = FakeBucket()
    s3.put("old-bucket", "empty.txt", b"")
    s3.put("old-bucket", "a/b/c/binary.bin", bytes(range(256)))
    s3.put(
        "old-bucket",
        "avatars/1.png",
        b"png",
        headers={"ContentType": "image/png", "CacheControl": "max-age=60", "ContentDisposition": "inline"},
        metadata={"owner": "u1"},
        tags={"kind": "avatar", "empty": ""},
    )
    s3.put("old-bucket", "gzip.json", b"{}", headers={"ContentEncoding": "gzip", "ContentLanguage": "en"})
    s3.put("old-bucket", "z.txt", b"last")
    return clients


def test_bucket_copy_preserves_bytes_headers_metadata_and_tags(buckets: FakeClients) -> None:
    report = transfer_bucket(buckets, OLD, NEW, apply=True, include_versions=False)

    assert report.outcome is Outcome.COPIED, report.to_text()
    copied = buckets.s3_client.buckets["new-bucket"].objects
    assert set(copied) == set(buckets.s3_client.buckets["old-bucket"].objects)
    avatar = copied["avatars/1.png"][-1]
    assert avatar.body == b"png"
    assert avatar.headers == {"ContentType": "image/png", "CacheControl": "max-age=60", "ContentDisposition": "inline"}
    assert avatar.metadata == {"owner": "u1"}
    assert avatar.tags == {"kind": "avatar", "empty": ""}
    assert copied["a/b/c/binary.bin"][-1].body == bytes(range(256))
    assert (report.counts.source_total, report.counts.target_total, report.counts.written) == (5, 5, 5)


def test_bucket_dry_run_writes_nothing(buckets: FakeClients) -> None:
    report = transfer_bucket(buckets, OLD, NEW, apply=False, include_versions=False)

    assert report.outcome is Outcome.CHECKED
    assert report.counts.source_total == 5
    assert buckets.s3_client.copies == []


def test_non_empty_or_missing_target_bucket_refuses(buckets: FakeClients) -> None:
    buckets.s3_client.put("new-bucket", "already-here")
    assert transfer_bucket(buckets, OLD, NEW, apply=True, include_versions=False).outcome is Outcome.REFUSED
    missing = parse_bucket_arn("arn:aws:s3:::missing-bucket")
    assert transfer_bucket(buckets, OLD, missing, apply=True, include_versions=False).outcome is Outcome.REFUSED
    assert buckets.s3_client.copies == []


def test_failed_objects_fail_the_transfer(buckets: FakeClients) -> None:
    buckets.s3_client.fail_copy.add("z.txt")

    report = transfer_bucket(buckets, OLD, NEW, apply=True, include_versions=False)

    assert report.outcome is Outcome.FAILED
    assert report.counts.failed == 1
    assert any(f.startswith("z.txt") for f in report.failures)


def test_versions_are_copied_oldest_first(buckets: FakeClients) -> None:
    s3 = buckets.s3_client
    s3.buckets["old-bucket"].versioning = "Enabled"
    s3.buckets["new-bucket"].versioning = "Enabled"
    s3.put("old-bucket", "z.txt", b"newer")
    s3.buckets["old-bucket"].delete_markers = 1

    report = transfer_bucket(buckets, OLD, NEW, apply=True, include_versions=True)

    assert report.outcome is Outcome.COPIED, report.to_text()
    assert report.counts.versions == 6
    assert [body.body for body in s3.buckets["new-bucket"].objects["z.txt"]] == [b"last", b"newer"]
    assert any("delete marker" in note for note in report.notes)
    copied_ids = [version_id for _, version_id in s3.copies]
    assert copied_ids == sorted(copied_ids, key=lambda v: int(v[1:]))


def test_versions_need_a_versioned_target(buckets: FakeClients) -> None:
    report = transfer_bucket(buckets, OLD, NEW, apply=True, include_versions=True)
    assert report.outcome is Outcome.REFUSED
    assert "versioning" in report.reasons[0]


# ── CLI ─────────────────────────────────────────────────────────────


def test_cli_requires_a_mode(clients: FakeClients) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["table", "--source", SOURCE.arn, "--target", TARGET.arn], clients=clients)
    assert caught.value.code == 2


def test_cli_dry_run_and_apply_exit_codes(clients: FakeClients, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["table", "--source", SOURCE.arn, "--target", TARGET.arn, "--dry-run"], clients=clients) == 0
    assert clients.dynamo.writes == []
    capsys.readouterr()
    assert main(["table", "--source", SOURCE.arn, "--target", TARGET.arn, "--apply", "--format", "json"], clients=clients) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "COPIED"


def test_cli_refusal_exits_1(clients: FakeClients) -> None:
    clients.dynamo.tables["new-users"].items.append({"pk": {"S": "x"}, "sk": {"S": "y"}})
    assert main(["table", "--source", SOURCE.arn, "--target", TARGET.arn, "--apply"], clients=clients) == 1


def test_cli_rejects_a_table_resource_in_resolve_copy(clients: FakeClients) -> None:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "resolve-copy",
                "--source-layout",
                "legacy",
                "--source-env",
                "preview",
                "--source-resource",
                "UsersTable",
                "--target-env",
                "dev",
                "--target-resource",
                "UserCorpusBucket",
                "--dry-run",
            ],
            clients=clients,
        )
    assert caught.value.code == 2
