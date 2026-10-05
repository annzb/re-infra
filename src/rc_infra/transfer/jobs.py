"""The two transfer jobs. Every check runs before the first write, and a dry run stops there."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.exceptions import ClientError

from rc_infra.transfer import dynamodb, s3
from rc_infra.transfer.arns import BucketArn, TableArn
from rc_infra.transfer.manifest import Manifest, ManifestError
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


def transfer_bucket(
    clients: Clients,
    source: BucketArn,
    target: BucketArn,
    *,
    apply: bool,
    include_versions: bool,
    manifest_path: Path | None = None,
    verify_content: bool = False,
) -> Report:
    report = Report("bucket", source.arn, target.arn, apply)
    started = time.monotonic()
    phase = ["checking"]
    try:
        _transfer_bucket(clients, source, target, report, include_versions, manifest_path, verify_content, phase)
    except KeyboardInterrupt:
        report.outcome = Outcome.INTERRUPTED
        report.reasons.append(
            f"interrupted; {report.counts.written} object(s) already written to the target are kept and recorded in the manifest. "
            "Run the same command again to resume."
        )
    except ManifestError as exc:
        report.refuse(str(exc))
    except ClientError as exc:
        # Never let an AWS error escape as a traceback, or pass for a completed copy.
        report.fail(f"while {phase[0]}: {exc}")
    report.elapsed_seconds = time.monotonic() - started
    return report


def _transfer_bucket(
    clients: Clients,
    source: BucketArn,
    target: BucketArn,
    report: Report,
    include_versions: bool,
    manifest_path: Path | None,
    verify_content: bool,
    phase: list[str],
) -> None:
    if source.name == target.name:
        report.refuse("source and target are the same bucket")
        return
    if report.apply and manifest_path is None:
        report.refuse(
            "--manifest is required with --apply: it records every version copied, maps old version IDs to new, and lets an interrupted copy resume"
        )
        return
    client = clients.s3()
    missing = [arn.name for arn in (source, target) if not s3.exists(client, arn.name)]
    if missing:
        report.refuse(f"bucket not found: {', '.join(missing)}")
        return
    if include_versions and client.get_bucket_versioning(Bucket=target.name).get("Status") != "Enabled":
        report.refuse("--include-versions needs versioning enabled on the target, or only the last version of each key would survive")
        return

    manifest = Manifest(manifest_path, source.name, target.name) if manifest_path else None
    if manifest is not None and manifest.entries:
        # Resuming: the target may hold exactly what the manifest recorded, nothing more.
        if include_versions:
            unaccounted = s3.all_version_ids(client, target.name) != manifest.target_versions()
        else:
            recorded = {e.key for e in manifest.entries.values() if not e.delete_marker}
            unaccounted = set(s3.current_state(client, target.name)) != recorded
        if unaccounted:
            report.refuse("the target holds objects the manifest does not account for; refusing to resume into it")
            return
        report.notes.append(f"resuming: {len(manifest.entries)} entr(ies) in {manifest_path} were already copied and are skipped")
    elif not s3.is_empty(client, target.name):
        report.refuse("target bucket is not empty (counting old versions and delete markers); transfers only write into an empty bucket")
        return

    phase[0] = "listing the source"
    if include_versions:
        items = s3.history(client, source.name)
        markers = sum(1 for item in items if item.delete_marker)
        report.counts.versions = len(items) - markers
        report.counts.delete_markers = markers
        report.notes.append("target version IDs are new; the manifest maps each source version to its target version")
    else:
        items = list(s3.current_objects(client, source.name))
    source_state = s3.current_state(client, source.name)
    report.counts.source_total, report.counts.bytes = len(source_state), sum(source_state.values())
    if not report.apply:
        return
    assert manifest is not None

    phase[0] = "copying"
    for item in items:
        if (item.key, item.version_id) in manifest:
            report.counts.skipped += 1
            continue
        try:
            if item.delete_marker:
                target_version = s3.delete_object(client, target.name, item)
            else:
                target_version = s3.copy_object(client, source.name, item, target.name)
        except ClientError as exc:
            report.counts.failed += 1
            report.failures.append(f"{item.key}{f' ({item.version_id})' if item.version_id else ''}: {exc}")
            continue
        manifest.record(item.key, item.version_id, target_version, delete_marker=item.delete_marker)
        report.counts.written += 1

    phase[0] = "verifying"
    report.failures.extend(_verify_bucket(client, source.name, target.name, source_state, manifest, include_versions, verify_content))
    target_state = s3.current_state(client, target.name)
    report.counts.target_total = len(target_state)
    if s3.current_state(client, source.name) != source_state:
        report.failures.append("the source changed during the copy; stop writes to it and copy again into a fresh target")
    if report.failures:
        report.fail(f"{len(report.failures)} problem(s) copying or verifying objects")
    else:
        report.outcome = Outcome.COPIED
        if verify_content:
            report.notes.append("every copied object's content was verified by SHA-256")


def _verify_bucket(
    client: S3Client,
    source: str,
    target: str,
    source_state: dict[str, int],
    manifest: Manifest,
    include_versions: bool,
    content: bool,
) -> list[str]:
    problems: list[str] = []
    # Current visibility: the same keys are current, with the same sizes. A key deleted in
    # the source must not have come back to life in the target.
    target_state = s3.current_state(client, target)
    for key in sorted(source_state.keys() - target_state.keys()):
        problems.append(f"{key}: current in the source, not in the target")
    for key in sorted(target_state.keys() - source_state.keys()):
        problems.append(f"{key}: current in the target, not in the source")
    if include_versions:
        # Every version the manifest recorded, against the exact target version it became.
        for entry in manifest.entries.values():
            if not entry.delete_marker:
                item = s3.SourceObject(entry.key, 0, entry.source_version)
                problems.extend(s3.verify_object(client, source, item, target, target_version=entry.target_version, content=content))
    else:
        for key in sorted(source_state.keys() & target_state.keys()):
            problems.extend(s3.verify_object(client, source, s3.SourceObject(key, source_state[key]), target, content=content))
    return problems
