"""S3 bucket operations and the live-versus-template comparison that gates imports.

A CloudFormation import records the template's properties as the bucket's state
without applying them. If they differ from the live bucket, a later update would
silently reconfigure a bucket holding production data. rc-infra therefore imports a
bucket only when its live configuration matches the template exactly, for every
property group the template declares or that would otherwise be reset.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

CFN_STACK_NAME_TAG = "aws:cloudformation:stack-name"

# Property groups the comparison understands. A template property outside this set
# fails loudly so the comparison is extended instead of silently skipped.
COMPARED_PROPERTIES = frozenset(
    {
        "BucketEncryption",
        "PublicAccessBlockConfiguration",
        "VersioningConfiguration",
        "LifecycleConfiguration",
        "CorsConfiguration",
    }
)

_MISSING_CONFIGURATION_ERRORS = frozenset(
    {
        "ServerSideEncryptionConfigurationNotFoundError",
        "NoSuchPublicAccessBlockConfiguration",
        "NoSuchLifecycleConfiguration",
        "NoSuchCORSConfiguration",
        "NoSuchTagSet",
    }
)


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

    def owner_stack(self, name: str) -> str | None:
        response = self._optional(lambda: self._s3.get_bucket_tagging(Bucket=name))
        tags = {tag["Key"]: tag["Value"] for tag in (response or {}).get("TagSet", [])}
        return tags.get(CFN_STACK_NAME_TAG)

    def live_config(self, name: str) -> dict[str, Any]:
        return {
            "encryption": self._optional(lambda: self._s3.get_bucket_encryption(Bucket=name)),
            "public_access_block": self._optional(
                lambda: self._s3.get_public_access_block(Bucket=name)
            ),
            "versioning": self._s3.get_bucket_versioning(Bucket=name),
            "lifecycle": self._optional(
                lambda: self._s3.get_bucket_lifecycle_configuration(Bucket=name)
            ),
            "cors": self._optional(lambda: self._s3.get_bucket_cors(Bucket=name)),
        }

    def empty_and_delete(self, name: str) -> None:
        if not self.exists(name):
            return
        paginator = self._s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=name):
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
            ]
            # list_object_versions pages hold at most 1000 entries, delete_objects' limit.
            if objects:
                self._s3.delete_objects(Bucket=name, Delete={"Objects": objects, "Quiet": True})  # type: ignore[typeddict-item]
        self._s3.delete_bucket(Bucket=name)

    @staticmethod
    def _optional(call: Any) -> Any:
        try:
            return call()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in _MISSING_CONFIGURATION_ERRORS:
                return None
            raise


def config_diff(template_properties: Mapping[str, Any], live: Mapping[str, Any]) -> list[str]:
    """Human-readable differences between a template's bucket properties and a live bucket."""
    unsupported = sorted(set(template_properties) - COMPARED_PROPERTIES)
    if unsupported:
        raise ValueError(f"cannot compare bucket properties {unsupported}; extend buckets.py")

    expected = normalize_template(template_properties)
    actual = normalize_live(live)
    return [
        f"{group}: template={expected[group]!r} live={actual[group]!r}"
        for group in sorted(expected)
        if expected[group] != actual[group]
    ]


def normalize_template(properties: Mapping[str, Any]) -> dict[str, Any]:
    encryption = properties.get("BucketEncryption", {}).get("ServerSideEncryptionConfiguration", [])
    block = properties.get("PublicAccessBlockConfiguration", {})
    lifecycle = properties.get("LifecycleConfiguration", {}).get("Rules", [])
    cors = properties.get("CorsConfiguration", {}).get("CorsRules", [])
    return {
        "encryption": sorted(
            (
                rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"],
                rule["ServerSideEncryptionByDefault"].get("KMSMasterKeyID"),
            )
            for rule in encryption
        ),
        "public_access_block": _block(block),
        "versioning": properties.get("VersioningConfiguration", {}).get("Status", "Disabled"),
        "lifecycle": sorted(
            (
                _lifecycle_rule(
                    rule_id=rule.get("Id"),
                    status=rule.get("Status"),
                    prefix=rule.get("Prefix"),
                    expiration_days=rule.get("ExpirationInDays"),
                    noncurrent_days=rule.get("NoncurrentVersionExpirationInDays"),
                    abort_multipart_days=rule.get("AbortIncompleteMultipartUpload", {}).get(
                        "DaysAfterInitiation"
                    ),
                    other=sorted(set(rule) - _TEMPLATE_LIFECYCLE_KEYS),
                )
                for rule in lifecycle
            ),
            key=repr,
        ),
        "cors": sorted(
            (
                _cors_rule(
                    rule_id=rule.get("Id"),
                    headers=rule.get("AllowedHeaders", []),
                    methods=rule.get("AllowedMethods", []),
                    origins=rule.get("AllowedOrigins", []),
                    exposed=rule.get("ExposedHeaders", []),
                    max_age=rule.get("MaxAge"),
                )
                for rule in cors
            ),
            key=repr,
        ),
    }


def normalize_live(live: Mapping[str, Any]) -> dict[str, Any]:
    encryption = (live.get("encryption") or {}).get("ServerSideEncryptionConfiguration", {})
    block = (live.get("public_access_block") or {}).get("PublicAccessBlockConfiguration", {})
    lifecycle = (live.get("lifecycle") or {}).get("Rules", [])
    cors = (live.get("cors") or {}).get("CORSRules", [])
    return {
        "encryption": sorted(
            (
                rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"],
                rule["ApplyServerSideEncryptionByDefault"].get("KMSMasterKeyID"),
            )
            for rule in encryption.get("Rules", [])
        ),
        "public_access_block": _block(block),
        "versioning": (live.get("versioning") or {}).get("Status", "Disabled"),
        "lifecycle": sorted(
            (
                _lifecycle_rule(
                    rule_id=rule.get("ID"),
                    status=rule.get("Status"),
                    prefix=(rule.get("Filter") or {}).get("Prefix", rule.get("Prefix")) or None,
                    expiration_days=rule.get("Expiration", {}).get("Days"),
                    noncurrent_days=rule.get("NoncurrentVersionExpiration", {}).get(
                        "NoncurrentDays"
                    ),
                    abort_multipart_days=rule.get("AbortIncompleteMultipartUpload", {}).get(
                        "DaysAfterInitiation"
                    ),
                    other=sorted(set(rule) - _LIVE_LIFECYCLE_KEYS),
                )
                for rule in lifecycle
            ),
            key=repr,
        ),
        "cors": sorted(
            (
                _cors_rule(
                    rule_id=rule.get("ID"),
                    headers=rule.get("AllowedHeaders", []),
                    methods=rule.get("AllowedMethods", []),
                    origins=rule.get("AllowedOrigins", []),
                    exposed=rule.get("ExposeHeaders", []),
                    max_age=rule.get("MaxAgeSeconds"),
                )
                for rule in cors
            ),
            key=repr,
        ),
    }


_TEMPLATE_LIFECYCLE_KEYS = frozenset(
    {
        "Id",
        "Status",
        "Prefix",
        "ExpirationInDays",
        "NoncurrentVersionExpirationInDays",
        "AbortIncompleteMultipartUpload",
    }
)
_LIVE_LIFECYCLE_KEYS = frozenset(
    {
        "ID",
        "Status",
        "Prefix",
        "Filter",
        "Expiration",
        "NoncurrentVersionExpiration",
        "AbortIncompleteMultipartUpload",
    }
)


def _block(block: Mapping[str, Any]) -> dict[str, bool]:
    return {
        key: bool(block.get(key, False))
        for key in (
            "BlockPublicAcls",
            "BlockPublicPolicy",
            "IgnorePublicAcls",
            "RestrictPublicBuckets",
        )
    }


def _lifecycle_rule(**fields: Any) -> dict[str, Any]:
    return fields


def _cors_rule(
    *,
    rule_id: str | None,
    headers: list[str],
    methods: list[str],
    origins: list[str],
    exposed: list[str],
    max_age: int | None,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "headers": sorted(headers),
        "methods": sorted(methods),
        "origins": sorted(origins),
        "exposed": sorted(exposed),
        "max_age": max_age,
    }
