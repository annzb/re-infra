"""Strict parsing of the two kinds of ARN rc-data-transfer accepts."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TABLE_ARN = re.compile(
    r"^arn:(?P<partition>aws[a-z-]*):dynamodb:(?P<region>[a-z]{2}(-[a-z]+)+-\d):(?P<account>\d{12}):table/(?P<name>[A-Za-z0-9_.-]{3,255})$"
)
_BUCKET_ARN = re.compile(r"^arn:(?P<partition>aws[a-z-]*):s3:::(?P<name>[a-z0-9][a-z0-9.-]{1,61}[a-z0-9])$")


class ArnError(ValueError):
    pass


@dataclass(frozen=True)
class TableArn:
    arn: str
    region: str
    account: str
    name: str


@dataclass(frozen=True)
class BucketArn:
    arn: str
    name: str


def parse_table_arn(arn: str) -> TableArn:
    match = _TABLE_ARN.match(arn)
    if match is None:
        if ":dynamodb:" in arn and "/index/" in arn:
            raise ArnError(f"{arn} is an index ARN; pass the table ARN")
        raise ArnError(f"{arn} is not a DynamoDB table ARN (arn:aws:dynamodb:<region>:<account>:table/<name>)")
    return TableArn(arn=arn, region=match["region"], account=match["account"], name=match["name"])


def parse_bucket_arn(arn: str) -> BucketArn:
    match = _BUCKET_ARN.match(arn)
    if match is None:
        if arn.startswith("arn:") and ":s3:::" in arn and "/" in arn:
            raise ArnError(f"{arn} is an object ARN; pass the bucket ARN")
        raise ArnError(f"{arn} is not an S3 bucket ARN (arn:aws:s3:::<bucket>)")
    return BucketArn(arn=arn, name=match["name"])


def table_arn(region: str, account: str, name: str) -> str:
    return f"arn:aws:dynamodb:{region}:{account}:table/{name}"


def bucket_arn(name: str) -> str:
    return f"arn:aws:s3:::{name}"
