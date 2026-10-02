"""The two transfer jobs. Every check runs before the first write, and a dry run stops there."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.exceptions import ClientError

from rc_infra.transfer import dynamodb, s3
from rc_infra.transfer.arns import BucketArn, TableArn
from rc_infra.transfer.models import Outcome, Report

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_s3 import S3Client


class Clients(Protocol):
    def account(self) -> str: ...

    def dynamodb(self, region: str) -> DynamoDBClient: ...

    def s3(self) -> S3Client: ...


class BotoClients:
    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session()

    def account(self) -> str:
        return str(self._session.client("sts").get_caller_identity()["Account"])

    def dynamodb(self, region: str) -> DynamoDBClient:
        return self._session.client("dynamodb", region_name=region)

    def s3(self) -> S3Client:
        return self._session.client("s3")


def transfer_table(
    clients: Clients,
    source: TableArn,
    target: TableArn,
    *,
    apply: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    report = Report("table", source.arn, target.arn, apply)
    started = time.monotonic()
    try:
        _transfer_table(clients, source, target, report, sleep)
    except KeyboardInterrupt:
        report.outcome = Outcome.INTERRUPTED
        report.reasons.append(f"interrupted; {report.counts.written} item(s) already written to the target are kept")
    report.elapsed_seconds = time.monotonic() - started
    return report


def _transfer_table(clients: Clients, source: TableArn, target: TableArn, report: Report, sleep: Callable[[float], None]) -> None:
    if source.arn == target.arn:
        report.refuse("source and target are the same table")
        return
    account = clients.account()
    foreign = sorted({arn.account for arn in (source, target)} - {account})
    if foreign:
        report.refuse(f"credentials are for account {account}; cross-account transfer ({', '.join(foreign)}) is not supported")
        return

    source_client, target_client = clients.dynamodb(source.region), clients.dynamodb(target.region)
    try:
        report.diffs = [
            *dynamodb.diff(dynamodb.canonical_config(source_client, source.name), dynamodb.canonical_config(target_client, target.name)),
            *dynamodb.diff(
                dynamodb.advisory_config(source_client, source.name),
                dynamodb.advisory_config(target_client, target.name),
                blocking=False,
            ),
        ]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            report.refuse(f"table not found: {exc.response['Error'].get('Message', '')}")
            return
        raise
    if any(d.blocking for d in report.diffs):
        report.refuse("DynamoDB configurations differ.")
        return
    if not dynamodb.is_empty(target_client, target.name):
        report.refuse("target table is not empty; transfers only write into an empty table")
        return

    report.counts.source_total = dynamodb.count(source_client, source.name)
    if not report.apply:
        return

    try:
        dynamodb.copy_items(source_client, source.name, target_client, target.name, report.counts, sleep)
    except dynamodb.CopyFailed as exc:
        report.fail(str(exc))
        return
    report.counts.target_total = dynamodb.count(target_client, target.name)
    if report.counts.target_total != report.counts.scanned:
        report.fail(f"target holds {report.counts.target_total} item(s) but {report.counts.scanned} were scanned from the source")
    elif report.counts.scanned != report.counts.source_total:
        report.fail(
            f"the source changed during the copy ({report.counts.source_total} item(s) before, {report.counts.scanned} scanned); "
            "stop writes to the source and copy again into a fresh target"
        )
    else:
        report.outcome = Outcome.COPIED


def transfer_bucket(clients: Clients, source: BucketArn, target: BucketArn, *, apply: bool, include_versions: bool) -> Report:
    report = Report("bucket", source.arn, target.arn, apply)
    started = time.monotonic()
    try:
        _transfer_bucket(clients, source, target, report, include_versions)
    except KeyboardInterrupt:
        report.outcome = Outcome.INTERRUPTED
        report.reasons.append(f"interrupted; {report.counts.written} object(s) already written to the target are kept")
    report.elapsed_seconds = time.monotonic() - started
    return report


def _transfer_bucket(clients: Clients, source: BucketArn, target: BucketArn, report: Report, include_versions: bool) -> None:
    if source.name == target.name:
        report.refuse("source and target are the same bucket")
        return
    client = clients.s3()
    missing = [arn.name for arn in (source, target) if not s3.exists(client, arn.name)]
    if missing:
        report.refuse(f"bucket not found: {', '.join(missing)}")
        return
    if not s3.is_empty(client, target.name, include_versions=include_versions):
        report.refuse("target bucket is not empty; transfers only write into an empty bucket")
        return
    if include_versions and client.get_bucket_versioning(Bucket=target.name).get("Status") != "Enabled":
        report.refuse("--include-versions needs versioning enabled on the target, or only the last version of each key would survive")
        return

    if include_versions:
        items, delete_markers = s3.all_versions(client, source.name)
        report.counts.versions = len(items)
        report.notes.append("target version IDs are new; they cannot equal the source's")
        if delete_markers:
            report.notes.append(f"{delete_markers} delete marker(s) in the source are not reproduced")
    else:
        items = list(s3.current_objects(client, source.name))
    source_count, source_bytes = s3.totals(client, source.name)
    report.counts.source_total, report.counts.bytes = source_count, source_bytes
    if not report.apply:
        return

    for item in items:
        try:
            s3.copy_object(client, source.name, item, target.name)
        except ClientError as exc:
            report.counts.failed += 1
            report.failures.append(f"{item.key}{f' ({item.version_id})' if item.version_id else ''}: {exc}")
        else:
            report.counts.written += 1

    for item in s3.current_objects(client, source.name):
        report.failures.extend(s3.verify_object(client, source.name, item, target.name))
    target_count, target_bytes = s3.totals(client, target.name)
    report.counts.target_total = target_count
    if (target_count, target_bytes) != (source_count, source_bytes):
        report.failures.append(f"target holds {target_count} object(s) / {target_bytes} bytes; source holds {source_count} / {source_bytes}")
    if report.failures:
        report.fail(f"{len(report.failures)} problem(s) copying or verifying objects")
    else:
        report.outcome = Outcome.COPIED
