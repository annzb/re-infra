from __future__ import annotations

import copy
from typing import Any

import pytest

from rc_identity import handlers
from rc_identity.backfill import backfill
from rc_identity.directory import AccountDirectory, ConflictingLink, Login, issuer_for
from tests.fakes import FakeCognito, FakeDynamo

POOL = "us-east-1_LwH5lWQ0q"
ISSUER = issuer_for("us-east-1", POOL)


@pytest.fixture
def dynamo() -> FakeDynamo:
    return FakeDynamo()


@pytest.fixture
def directory(dynamo: FakeDynamo) -> AccountDirectory:
    return AccountDirectory("directory", dynamo)


@pytest.fixture
def cognito(monkeypatch: pytest.MonkeyPatch, directory: AccountDirectory) -> FakeCognito:
    fake = FakeCognito()
    monkeypatch.setattr(handlers, "_directory", directory)
    monkeypatch.setattr(handlers, "_cognito", fake)
    monkeypatch.setenv("IDENTITY_PROFILE", "preview")
    return fake


def _event(trigger: str, **attributes: str) -> dict[str, Any]:
    return {
        "triggerSource": trigger,
        "region": "us-east-1",
        "userPoolId": POOL,
        "userName": "user-1",
        "request": {"userAttributes": {"sub": "sub-1", **attributes}},
        "response": {},
    }


# ── directory ───────────────────────────────────────────────────────


def test_link_is_idempotent(directory: AccountDirectory) -> None:
    login = Login(ISSUER, "sub-1")
    assert directory.link("acct-1", login, profile="preview") == "acct-1"
    assert directory.link("acct-1", login, profile="preview") == "acct-1"
    assert directory.account_for(login) == "acct-1"


def test_a_login_never_moves_to_another_account(directory: AccountDirectory) -> None:
    login = Login(ISSUER, "sub-1")
    directory.link("acct-1", login, profile="preview")
    with pytest.raises(ConflictingLink):
        directory.link("acct-2", login, profile="preview")
    assert directory.account_for(login) == "acct-1"


def test_logins_are_scoped_by_issuer(directory: AccountDirectory) -> None:
    """The same subject string in another pool is a different login."""
    directory.link("acct-preview", Login(ISSUER, "sub-1"), profile="preview")
    directory.link("acct-prod", Login(issuer_for("us-east-1", "us-east-1_prod"), "sub-1"), profile="prod")
    assert directory.account_for(Login(ISSUER, "sub-1")) == "acct-preview"


def test_two_logins_can_share_an_account(directory: AccountDirectory, dynamo: FakeDynamo) -> None:
    directory.link("acct-1", Login(ISSUER, "sub-1"), profile="preview")
    directory.link("acct-1", Login(ISSUER, "sub-2"), profile="preview", provider="Google")
    assert dynamo.items[("ACCOUNT#acct-1", "ACCOUNT")]["status"] == {"S": "active"}
    assert dynamo.items[("LOGIN#" + ISSUER + "#sub-2", "LOGIN")]["provider"] == {"S": "Google"}


# ── post-confirmation ───────────────────────────────────────────────


def test_existing_account_id_is_kept(cognito: FakeCognito, directory: AccountDirectory) -> None:
    event = _event("PostConfirmation_ConfirmSignUp", **{"custom:user_id": "acct-existing"})
    assert handlers.post_confirmation(event) == event
    assert directory.account_for(Login(ISSUER, "sub-1")) == "acct-existing"
    assert cognito.updates == []


def test_new_user_gets_an_account_id_attribute(cognito: FakeCognito, directory: AccountDirectory) -> None:
    handlers.post_confirmation(_event("PostConfirmation_ConfirmSignUp", email="a@example.com"))

    account_id = directory.account_for(Login(ISSUER, "sub-1"))
    assert account_id
    assert cognito.updates == [{"UserPoolId": POOL, "Username": "user-1", "UserAttributes": [{"Name": "custom:user_id", "Value": account_id}]}]


def test_retried_confirmation_reuses_the_same_account(cognito: FakeCognito, directory: AccountDirectory) -> None:
    event = _event("PostConfirmation_ConfirmSignUp")
    handlers.post_confirmation(copy.deepcopy(event))
    handlers.post_confirmation(copy.deepcopy(event))

    values = {update["UserAttributes"][0]["Value"] for update in cognito.updates}
    assert len(values) == 1


def test_forgot_password_confirmation_changes_nothing(cognito: FakeCognito, dynamo: FakeDynamo) -> None:
    handlers.post_confirmation(_event("PostConfirmation_ConfirmForgotPassword"))
    assert dynamo.items == {} and cognito.updates == []


def test_federated_provider_is_recorded(cognito: FakeCognito, dynamo: FakeDynamo) -> None:
    identities = '[{"providerName":"Google","userId":"123"}]'
    handlers.post_confirmation(_event("PostConfirmation_ConfirmSignUp", identities=identities))
    login = next(item for key, item in dynamo.items.items() if key[1] == "LOGIN")
    assert login["provider"] == {"S": "Google"}


# ── pre-token generation ────────────────────────────────────────────


def test_token_claim_comes_from_the_attribute_when_present(cognito: FakeCognito) -> None:
    event = _event("TokenGeneration_Authentication", **{"custom:user_id": "acct-1"})
    assert "claimsOverrideDetails" not in handlers.pre_token_generation(event)["response"]


def test_first_token_gets_the_claim_from_the_directory(cognito: FakeCognito, directory: AccountDirectory) -> None:
    directory.link("acct-1", Login(ISSUER, "sub-1"), profile="preview")
    event = handlers.pre_token_generation(_event("TokenGeneration_HostedAuth"))
    assert event["response"]["claimsOverrideDetails"] == {"claimsToAddOrOverride": {"custom:user_id": "acct-1"}}


def test_an_unmapped_login_gets_no_token(cognito: FakeCognito) -> None:
    with pytest.raises(RuntimeError):
        handlers.pre_token_generation(_event("TokenGeneration_Authentication"))


# ── backfill ────────────────────────────────────────────────────────


def _user(name: str, sub: str, account: str | None) -> dict[str, Any]:
    attributes = [{"Name": "sub", "Value": sub}]
    if account:
        attributes.append({"Name": "custom:user_id", "Value": account})
    return {"Username": name, "Attributes": attributes}


def test_backfill_links_existing_accounts_and_reports_the_rest(directory: AccountDirectory) -> None:
    cognito = FakeCognito([_user("a", "sub-a", "acct-a"), _user("b", "sub-b", None), _user("c", "sub-c", "acct-c")])

    dry = backfill(cognito, None, region="us-east-1", user_pool_id=POOL, profile="preview")
    assert dry.linked == 2 and directory.account_for(Login(ISSUER, "sub-a")) is None

    report = backfill(cognito, directory, region="us-east-1", user_pool_id=POOL, profile="preview")
    assert report.linked == 2
    assert report.without_account == ["b"]
    assert directory.account_for(Login(ISSUER, "sub-c")) == "acct-c"


def test_backfill_reports_conflicts_without_overwriting(directory: AccountDirectory) -> None:
    directory.link("acct-other", Login(ISSUER, "sub-a"), profile="preview")
    report = backfill(FakeCognito([_user("a", "sub-a", "acct-a")]), directory, region="us-east-1", user_pool_id=POOL, profile="preview")
    assert len(report.conflicts) == 1
    assert directory.account_for(Login(ISSUER, "sub-a")) == "acct-other"
