"""Copy the objects of one S3 bucket into an empty bucket.

Only data moves. The target's configuration (encryption, lifecycle, CORS, versioning)
is whatever its CloudFormation template declares; nothing here changes it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

# Object properties that travel with the copy, set explicitly rather than relying on
# how a particular s3transfer version treats single-part versus multipart copies.
PRESERVED_HEADERS = ("ContentType", "ContentEncoding", "CacheControl", "ContentDisposition", "ContentLanguage")


@dataclass(frozen=True)
class SourceObject:
    key: str
    size: int
    version_id: str | None = None
    last_modified: datetime | None = None


def exists(client: S3Client, bucket: str) -> bool:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchBucket", "NotFound"}:
            return False
        raise
    return True


def is_empty(client: S3Client, bucket: str, include_versions: bool = False) -> bool:
    if client.list_objects_v2(Bucket=bucket, MaxKeys=1).get("KeyCount", 0):
        return False
    if not include_versions:
        return True
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


def all_versions(client: S3Client, bucket: str) -> tuple[list[SourceObject], int]:
    """Every object version, oldest first, so the target's history ends with the same
    current version. Delete markers are counted and skipped: they are not recreated."""
    versions: list[SourceObject] = []
    delete_markers = 0
    markers: dict[str, Any] = {}
    while True:
        page = client.list_object_versions(Bucket=bucket, **markers)
        versions.extend(
            SourceObject(key=item["Key"], size=item["Size"], version_id=item["VersionId"], last_modified=item["LastModified"])
            for item in page.get("Versions", [])
        )
        delete_markers += len(page.get("DeleteMarkers", []))
        if not page.get("IsTruncated"):
            break
        markers = {"KeyMarker": page["NextKeyMarker"], "VersionIdMarker": page["NextVersionIdMarker"]}
    versions.sort(key=lambda version: (version.last_modified or datetime.min, version.key))
    return versions, delete_markers


def copy_object(client: S3Client, source_bucket: str, item: SourceObject, target_bucket: str) -> None:
    """Copy one object (or one version) with its content headers, user metadata and tags.
    Multipart for large objects, through boto3's managed copy."""
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


def verify_object(client: S3Client, source_bucket: str, item: SourceObject, target_bucket: str) -> list[str]:
    """Differences between a copied object and its source. ETags are not compared: a
    multipart copy legitimately produces a different one."""
    version: dict[str, Any] = {"VersionId": item.version_id} if item.version_id else {}
    source: Mapping[str, Any] = client.head_object(Bucket=source_bucket, Key=item.key, **version)
    try:
        target: Mapping[str, Any] = client.head_object(Bucket=target_bucket, Key=item.key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return [f"{item.key}: missing from the target"]
        raise
    problems = []
    for field in ("ContentLength", *PRESERVED_HEADERS, "Metadata"):
        # An absent header and an empty one are the same thing.
        expected, actual = source.get(field) or None, target.get(field) or None
        if expected != actual:
            problems.append(f"{item.key}: {field} source={expected!r} target={actual!r}")
    source_tags = _tags(client, source_bucket, item.key, item.version_id)
    target_tags = _tags(client, target_bucket, item.key, None)
    if source_tags != target_tags:
        problems.append(f"{item.key}: tags source={source_tags!r} target={target_tags!r}")
    return problems


def totals(client: S3Client, bucket: str) -> tuple[int, int]:
    """Current object count and bytes."""
    objects = list(current_objects(client, bucket))
    return len(objects), sum(item.size for item in objects)


def _tags(client: S3Client, bucket: str, key: str, version_id: str | None) -> dict[str, str]:
    version: dict[str, Any] = {"VersionId": version_id} if version_id else {}
    return {tag["Key"]: tag["Value"] for tag in client.get_object_tagging(Bucket=bucket, Key=key, **version)["TagSet"]}
