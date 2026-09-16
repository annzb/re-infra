"""DynamoDB operations used by teardown. Tables are owned by rc-dynamo-sync, not CloudFormation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

MANAGED_BY_TAG = "ManagedBy"
MANAGED_BY_VALUE = "rc-dynamo-sync"
ENVIRONMENT_TAG = "Environment"


class DynamoTables:
    def __init__(self, client: DynamoDBClient) -> None:
        self._dynamodb = client

    def list_names(self, prefix: str) -> list[str]:
        names: list[str] = []
        for page in self._dynamodb.get_paginator("list_tables").paginate():
            names.extend(name for name in page.get("TableNames", []) if name.startswith(prefix))
        return sorted(names)

    def tags(self, name: str) -> dict[str, str]:
        arn = self._dynamodb.describe_table(TableName=name)["Table"]["TableArn"]
        tags: dict[str, str] = {}
        for page in self._dynamodb.get_paginator("list_tags_of_resource").paginate(ResourceArn=arn):
            tags.update({tag["Key"]: tag["Value"] for tag in page.get("Tags", [])})
        return tags

    def delete(self, name: str) -> None:
        try:
            self._dynamodb.delete_table(TableName=name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        self._dynamodb.get_waiter("table_not_exists").wait(TableName=name)


def is_managed_by_environment(tags: dict[str, str], environment: str) -> bool:
    return tags.get(MANAGED_BY_TAG) == MANAGED_BY_VALUE and tags.get(ENVIRONMENT_TAG) == environment
