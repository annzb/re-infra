"""rc-identity-backfill: fill the account directory from a pool's existing users.

    rc-identity-backfill --user-pool-id us-east-1_XXXX --profile preview --table T          # dry run
    rc-identity-backfill --user-pool-id us-east-1_XXXX --profile preview --table T --yes

Every user's custom:user_id is the account ID its application records already use,
so it is recorded as is, linked to the user's (issuer, sub). A user without one is
reported, never given an invented ID: that is the post-confirmation handler's job.
Idempotent; a conflicting existing link is reported and left alone.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import boto3

from rc_identity.directory import AccountDirectory, ConflictingLink, Login, issuer_for
from rc_identity.handlers import ACCOUNT_ATTRIBUTE, _provider


@dataclass
class BackfillReport:
    linked: int = 0
    without_account: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def users(cognito: Any, user_pool_id: str) -> Iterator[dict[str, Any]]:
    for page in cognito.get_paginator("list_users").paginate(UserPoolId=user_pool_id):
        yield from page["Users"]


def backfill(cognito: Any, directory: AccountDirectory | None, *, region: str, user_pool_id: str, profile: str) -> BackfillReport:
    """With directory None, only count (dry run)."""
    issuer = issuer_for(region, user_pool_id)
    report = BackfillReport()
    for user in users(cognito, user_pool_id):
        attributes = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
        account_id = attributes.get(ACCOUNT_ATTRIBUTE)
        if not account_id:
            report.without_account.append(user["Username"])
            continue
        if directory is not None:
            try:
                directory.link(account_id, Login(issuer, attributes["sub"]), profile=profile, provider=_provider(attributes))
            except ConflictingLink as exc:
                report.conflicts.append(f"{user['Username']}: {exc}")
                continue
        report.linked += 1
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rc-identity-backfill", description=__doc__.splitlines()[0])
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--table", required=True, help="rc-identity's AccountDirectoryTableName output")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--yes", action="store_true", help="actually write")
    args = parser.parse_args(argv)

    cognito = boto3.client("cognito-idp", region_name=args.region)
    directory = AccountDirectory(args.table, boto3.client("dynamodb", region_name=args.region)) if args.yes else None
    report = backfill(cognito, directory, region=args.region, user_pool_id=args.user_pool_id, profile=args.profile)
    verb = "linked" if args.yes else "would link"
    print(f"{verb} {report.linked} users")
    print(f"{len(report.without_account)} users have no {ACCOUNT_ATTRIBUTE} (left for the post-confirmation handler)")
    for conflict in report.conflicts:
        print(f"conflict: {conflict}")
    if not args.yes:
        print("Dry run. Re-run with --yes to write.")
    return 1 if report.conflicts else 0
