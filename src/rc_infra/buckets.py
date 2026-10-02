"""S3 bucket operations used by status and teardown. The buckets themselves are rc-env-<env> stack resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class S3Buckets:
    def __init__(self, client: S3Client) -> None:
        self._s3 = client

    def exists(self, name: str) -> bool:
        try:
            self._s3.head_bucket(Bucket=name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchBucket", "NotFound"}:
                return False
            raise
        return True

    def is_empty(self, name: str, include_versions: bool = False) -> bool:
        if self._s3.list_objects_v2(Bucket=name, MaxKeys=1).get("KeyCount", 0):
            return False
        if not include_versions:
            return True
        versions = self._s3.list_object_versions(Bucket=name, MaxKeys=1)
        return not (versions.get("Versions") or versions.get("DeleteMarkers"))

    def empty_and_delete(self, name: str) -> None:
        if not self.exists(name):
            return
        paginator = self._s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=name):
            objects = [{"Key": item["Key"], "VersionId": item["VersionId"]} for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]]
            # list_object_versions pages hold at most 1000 entries, delete_objects' limit.
            if objects:
                self._s3.delete_objects(Bucket=name, Delete={"Objects": objects, "Quiet": True})  # type: ignore[typeddict-item]
        self._s3.delete_bucket(Bucket=name)
