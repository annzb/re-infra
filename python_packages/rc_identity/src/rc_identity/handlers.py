"""Cognito trigger handlers: identity only, never application data.

They replace the identity half of retribalize-core's post-signup function. Profile
creation, Supabase lookups, signup fan-out and marketing stay in core, which reads
the account ID from the token; identity must work with no core deployed at all.

Wiring them onto a pool is a later, explicit change of envs.yaml's
identity_profiles.<profile>.triggers (infra/identity.md).

Environment: ACCOUNT_DIRECTORY_TABLE, IDENTITY_PROFILE.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import boto3

from rc_identity.directory import AccountDirectory, Login, issuer_for

ACCOUNT_ATTRIBUTE = "custom:user_id"

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_directory: AccountDirectory | None = None
_cognito: Any = None


def _get_directory() -> AccountDirectory:
    global _directory
    if _directory is None:
        _directory = AccountDirectory(os.environ["ACCOUNT_DIRECTORY_TABLE"])
    return _directory


def _get_cognito() -> Any:
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


def _login(event: dict[str, Any]) -> Login:
    return Login(issuer=issuer_for(event["region"], event["userPoolId"]), subject=event["request"]["userAttributes"]["sub"])


def _provider(attributes: dict[str, str]) -> str | None:
    """The external provider of a federated user (from the identities attribute), else None."""
    try:
        identities = json.loads(attributes.get("identities") or "[]")
    except json.JSONDecodeError:
        return None
    return str(identities[0]["providerName"]) if identities else None


def post_confirmation(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Give a newly confirmed user a stable account ID and record the login.

    An existing custom:user_id is kept (it is the account ID every existing record
    uses); otherwise the directory's mapping, otherwise a new UUID. Retries and
    duplicate invocations converge on the same account.
    """
    if event.get("triggerSource") != "PostConfirmation_ConfirmSignUp":
        return event  # forgot-password confirmation: nothing about the identity changes
    attributes: dict[str, str] = event["request"]["userAttributes"]
    login = _login(event)
    directory = _get_directory()
    account_id = attributes.get(ACCOUNT_ATTRIBUTE) or directory.account_for(login) or str(uuid.uuid4())
    directory.link(account_id, login, profile=os.environ["IDENTITY_PROFILE"], provider=_provider(attributes))
    if attributes.get(ACCOUNT_ATTRIBUTE) != account_id:
        _get_cognito().admin_update_user_attributes(
            UserPoolId=event["userPoolId"],
            Username=event["userName"],
            UserAttributes=[{"Name": ACCOUNT_ATTRIBUTE, "Value": account_id}],
        )
    logger.info("linked login to account", extra={"account_id": account_id, "provider": _provider(attributes)})
    return event


def pre_token_generation(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Guarantee every token carries custom:user_id, including the very first one.

    The attribute normally carries it. Right after a federated first sign-in the
    attribute update may not be visible yet, so the claim comes from the directory.
    With neither, the sign-in fails rather than issue a token no service can map.
    """
    attributes: dict[str, str] = event["request"]["userAttributes"]
    if attributes.get(ACCOUNT_ATTRIBUTE):
        return event
    account_id = _get_directory().account_for(_login(event))
    if account_id is None:
        raise RuntimeError("no account is linked to this login")
    event.setdefault("response", {})["claimsOverrideDetails"] = {"claimsToAddOrOverride": {ACCOUNT_ATTRIBUTE: account_id}}
    return event
