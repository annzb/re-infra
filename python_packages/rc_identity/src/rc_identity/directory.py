"""The account directory: which stable application account a sign-in belongs to.

A login is the (issuer, subject) pair every token carries: the issuer names the user
pool, the subject the Cognito user in it. Scoping by issuer keeps the shared preview
pool's users apart from production's even when their emails match; nothing here ever
links accounts by email.

The directory holds identifiers only. Cognito remains the only credential authority.

Items (one table, rc-identity's AccountDirectoryTable):

    pk ACCOUNT#<account_id>       sk ACCOUNT   status, profile, created_at
    pk LOGIN#<issuer>#<subject>   sk LOGIN     account_id, profile, provider, created_at
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

ACCOUNT = "ACCOUNT"
LOGIN = "LOGIN"


class ConflictingLink(Exception):
    """The login already belongs to a different account. Never resolved automatically."""

    def __init__(self, issuer: str, subject: str, existing: str, requested: str) -> None:
        super().__init__(f"login {issuer}#{subject} belongs to account {existing}, not {requested}")
        self.existing = existing
        self.requested = requested


@dataclass(frozen=True)
class Login:
    issuer: str
    subject: str

    @property
    def key(self) -> dict[str, Any]:
        return {"pk": {"S": f"{LOGIN}#{self.issuer}#{self.subject}"}, "sk": {"S": LOGIN}}


def issuer_for(region: str, user_pool_id: str) -> str:
    """The `iss` claim of every token a pool issues."""
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def _account_key(account_id: str) -> dict[str, Any]:
    return {"pk": {"S": f"{ACCOUNT}#{account_id}"}, "sk": {"S": ACCOUNT}}


class AccountDirectory:
    def __init__(self, table_name: str, client: Any | None = None) -> None:
        self.table_name = table_name
        self._client = client if client is not None else boto3.client("dynamodb")

    def account_for(self, login: Login) -> str | None:
        item = self._client.get_item(TableName=self.table_name, Key=login.key, ConsistentRead=True).get("Item")
        return str(item["account_id"]["S"]) if item else None

    def link(self, account_id: str, login: Login, *, profile: str, provider: str | None = None) -> str:
        """Make the login belong to the account, creating the account if needed. Idempotent.

        Safe under retries and simultaneous first sign-ins: the login is written only if
        it is new or already points at this account, in the same transaction that
        ensures the account exists. A login that points elsewhere raises ConflictingLink.
        """
        now = {"N": str(int(time.time()))}
        login_item: dict[str, Any] = {
            **login.key,
            "account_id": {"S": account_id},
            "profile": {"S": profile},
            "created_at": now,
        }
        if provider:
            login_item["provider"] = {"S": provider}
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": _account_key(account_id),
                            "UpdateExpression": "SET #status = if_not_exists(#status, :active), "
                            "profile = if_not_exists(profile, :profile), created_at = if_not_exists(created_at, :now)",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {":active": {"S": "active"}, ":profile": {"S": profile}, ":now": now},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": login_item,
                            "ConditionExpression": "attribute_not_exists(pk) OR account_id = :account",
                            "ExpressionAttributeValues": {":account": {"S": account_id}},
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            existing = self.account_for(login)
            if existing is None:
                raise  # cancelled for another reason (throughput, a concurrent transaction): let the caller retry
            if existing != account_id:
                raise ConflictingLink(login.issuer, login.subject, existing, account_id) from exc
        return account_id
