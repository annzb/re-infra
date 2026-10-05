"""A DynamoDB client implementing just what AccountDirectory uses, with real condition semantics."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError


def _cancelled() -> ClientError:
    return ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "ConditionalCheckFailed"}}, "TransactWriteItems")


class FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions = 0

    @staticmethod
    def _key(key: dict[str, Any]) -> tuple[str, str]:
        return key["pk"]["S"], key["sk"]["S"]

    def get_item(self, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def transact_write_items(self, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self.transactions += 1
        # All conditions are checked before anything is written, as in DynamoDB.
        for operation in TransactItems:
            if "Put" in operation:
                put = operation["Put"]
                existing = self.items.get(self._key(put["Item"]))
                wanted = put["ExpressionAttributeValues"][":account"]["S"]
                if existing is not None and existing["account_id"]["S"] != wanted:
                    raise _cancelled()
        for operation in TransactItems:
            if "Put" in operation:
                item = operation["Put"]["Item"]
                self.items[self._key(item)] = dict(item)
            elif "Update" in operation:
                update = operation["Update"]
                key = self._key(update["Key"])
                current = self.items.setdefault(key, dict(update["Key"]))
                values = update["ExpressionAttributeValues"]
                current.setdefault("status", values[":active"])
                current.setdefault("profile", values[":profile"])
                current.setdefault("created_at", values[":now"])
        return {}


class FakeCognito:
    def __init__(self, users: list[dict[str, Any]] | None = None) -> None:
        self.updates: list[dict[str, Any]] = []
        self._users = users or []

    def admin_update_user_attributes(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def get_paginator(self, name: str) -> Any:
        assert name == "list_users"
        users = self._users

        class _Paginator:
            def paginate(self, **_: Any) -> list[dict[str, Any]]:
                return [{"Users": users[:1]}, {"Users": users[1:]}]

        return _Paginator()
