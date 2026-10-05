"""Copy the objects of one S3 bucket into an empty bucket.

Only data moves. The target's configuration (encryption, lifecycle, CORS, versioning)
is whatever its CloudFormation template declares; nothing here changes it.

With history, every version AND every delete marker is replayed per key in the
order it happened, so a key deleted in the source is deleted (not current) in the
target too, and each key's current version is the same. Target version IDs are new;
the manifest (manifest.py) maps each source version to the target version it became.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

# Object properties that travel with the copy, set explicitly rather than relying on
# how a particular s3transfer version treats single-part versus multipart copies.
PRESERVED_HEADERS = ("ContentType", "ContentEncoding", "CacheControl", "ContentDisposition", "ContentLanguage")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SourceObject:
    key: str
    size: int
    version_id: str | None = None
    last_modified: datetime | None = None
    delete_marker: bool = False


def exists(client: S3Client, bucket: str) -> bool:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchBucket", "NotFound"}:
            return False
        raise
    return True


def is_empty(client: S3Client, bucket: str) -> bool:
    """Pristine: no current objects, and no noncurrent versions or delete markers either."""
    if client.list_objects_v2(Bucket=bucket, MaxKeys=1).get("KeyCount", 0):
        return False
    versions = client.list_object_versions(Bucket=bucket, MaxKeys=1)
    return not (versions.get("Versions") or versions.get("DeleteMarkers"))


def current_objects(client: S3Client, bucket: str) -> Iterator[SourceObject]:
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for item in page.get("Contents", []):
            yield SourceObject(key=item["Key"], size=item["Size"])
        token = page.get("NextContinuationToken")
        if not token:
            return


def history(client: S3Client, bucket: str) -> list[SourceObject]:
    """Every version and delete marker: keys in order, each key's entries oldest first.

    Replaying this list in order reproduces each key's history, including which keys
    are deleted now. Entries with the same timestamp keep the latest one last.
    """
    by_key: dict[str, list[tuple[datetime, bool, SourceObject]]] = defaultdict(list)
    markers: dict[str, Any] = {}
    while True:
        page = client.list_object_versions(Bucket=bucket, **markers)
        for item in page.get("Versions", []):
            entry = SourceObject(item["Key"], item["Size"], item["VersionId"], item["LastModified"])
            by_key[item["Key"]].append((item["LastModified"], bool(item.get("IsLatest")), entry))
        for marker in page.get("DeleteMarkers", []):
            entry = SourceObject(marker["Key"], 0, marker["VersionId"], marker.get("LastModified"), delete_marker=True)
            by_key[marker["Key"]].append((marker.get("LastModified") or _EPOCH, bool(marker.get("IsLatest")), entry))
        if not page.get("IsTruncated"):
            break
        markers = {"KeyMarker": page["NextKeyMarker"], "VersionIdMarker": page["NextVersionIdMarker"]}
    return [entry for key in sorted(by_key) for _, _, entry in sorted(by_key[key], key=lambda e: (e[0], e[1]))]


def all_version_ids(client: S3Client, bucket: str) -> set[tuple[str, str]]:
    """(key, version ID) of every version and delete marker in a bucket."""
    return {(entry.key, entry.version_id or "null") for entry in history(client, bucket)}


def copy_object(client: S3Client, source_bucket: str, item: SourceObject, target_bucket: str) -> str | None:
    """Copy one object (or one version) with its content headers, user metadata and tags.
    Multipart for large objects, through boto3's managed copy. Returns the target's new version ID."""
    version: dict[str, Any] = {"VersionId": item.version_id} if item.version_id else {}
    head: Mapping[str, Any] = client.head_object(Bucket=source_bucket, Key=item.key, **version)
    tags = client.get_object_tagging(Bucket=source_bucket, Key=item.key, **version)["TagSet"]
    extra: dict[str, Any] = {header: head[header] for header in PRESERVED_HEADERS if head.get(header)}
    extra["Metadata"] = dict(head.get("Metadata", {}))
    extra["MetadataDirective"] = "REPLACE"
    extra["TaggingDirective"] = "REPLACE"
    extra["Tagging"] = urlencode([(tag["Key"], tag["Value"]) for tag in tags])
    copy_source: Any = {"Bucket": source_bucket, "Key": item.key, **version}
    client.copy(CopySource=copy_source, Bucket=target_bucket, Key=item.key, ExtraArgs=extra)
    # Nothing else writes to the target during a transfer, so its current version is this copy.
    return client.head_object(Bucket=target_bucket, Key=item.key).get("VersionId")


def delete_object(client: S3Client, target_bucket: str, item: SourceObject) -> str | None:
    """Replay a delete marker. In a versioned target this adds a marker; returns its version ID."""
    return client.delete_object(Bucket=target_bucket, Key=item.key).get("VersionId")


def verify_object(
    client: S3Client,
    source_bucket: str,
    item: SourceObject,
    target_bucket: str,
    *,
    target_version: str | None = None,
    content: bool = False,
) -> list[str]:
    """Differences between a copied object (or version) and its source.

    ETags are not compared: a multipart copy legitimately produces a different one.
    With content, both bodies are streamed and their SHA-256 digests compared.
    """
    label = f"{item.key}{f' ({item.version_id})' if item.version_id else ''}"
    source_version: dict[str, Any] = {"VersionId": item.version_id} if item.version_id else {}
    target_selector: dict[str, Any] = {"VersionId": target_version} if target_version else {}
    source: Mapping[str, Any] = client.head_object(Bucket=source_bucket, Key=item.key, **source_version)
    try:
        target: Mapping[str, Any] = client.head_object(Bucket=target_bucket, Key=item.key, **target_selector)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return [f"{label}: missing from the target"]
        raise
    problems = []
    for field in ("ContentLength", *PRESERVED_HEADERS, "Metadata"):
        # An absent header and an empty one are the same thing.
        expected, actual = source.get(field) or None, target.get(field) or None
        if expected != actual:
            problems.append(f"{label}: {field} source={expected!r} target={actual!r}")
    source_tags = _tags(client, source_bucket, item.key, item.version_id)
    target_tags = _tags(client, target_bucket, item.key, target_version)
    if source_tags != target_tags:
        problems.append(f"{label}: tags source={source_tags!r} target={target_tags!r}")
    if content and digest(client, source_bucket, item.key, item.version_id) != digest(client, target_bucket, item.key, target_version):
        problems.append(f"{label}: content differs")
    return problems


def digest(client: S3Client, bucket: str, key: str, version_id: str | None) -> str:
    version: dict[str, Any] = {"VersionId": version_id} if version_id else {}
    body = client.get_object(Bucket=bucket, Key=key, **version)["Body"]
    sha = hashlib.sha256()
    for chunk in iter(lambda: body.read(1024 * 1024), b""):
        sha.update(chunk)
    return sha.hexdigest()


def current_state(client: S3Client, bucket: str) -> dict[str, int]:
    """Current key -> size."""
    return {item.key: item.size for item in current_objects(client, bucket)}


def _tags(client: S3Client, bucket: str, key: str, version_id: str | None) -> dict[str, str]:
    version: dict[str, Any] = {"VersionId": version_id} if version_id else {}
    return {tag["Key"]: tag["Value"] for tag in client.get_object_tagging(Bucket=bucket, Key=key, **version)["TagSet"]}
