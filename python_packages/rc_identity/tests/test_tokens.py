from __future__ import annotations

import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from rc_identity.tokens import InvalidToken, TokenValidator

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"
CLIENT = "client-1"


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def validator(key: rsa.RSAPrivateKey) -> TokenValidator:
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "k1"
    fetched: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        fetched.append(url)
        return {"keys": [jwk]}

    validator = TokenValidator(ISSUER, CLIENT, fetch=fetch)
    validator.fetched = fetched  # type: ignore[attr-defined]
    return validator


def _token(key: rsa.RSAPrivateKey, kid: str = "k1", **overrides: Any) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "sub-1", "aud": CLIENT, "token_use": "id", "iat": now, "exp": now + 300, "custom:user_id": "acct-1"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def test_valid_id_token(validator: TokenValidator, key: rsa.RSAPrivateKey) -> None:
    claims = validator.validate(_token(key))
    assert claims["custom:user_id"] == "acct-1"
    assert validator.fetched == [f"{ISSUER}/.well-known/jwks.json"]  # type: ignore[attr-defined]


def test_valid_access_token(validator: TokenValidator, key: rsa.RSAPrivateKey) -> None:
    token = _token(key, aud=None, token_use="access", client_id=CLIENT)
    assert validator.validate(token, token_use="access")["sub"] == "sub-1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_other"},
        {"aud": "another-client"},
        {"exp": int(time.time()) - 10},
        {"token_use": "access"},
        {"exp": None},
    ],
    ids=["issuer", "audience", "expired", "token_use", "no-exp"],
)
def test_invalid_id_tokens_are_rejected(validator: TokenValidator, key: rsa.RSAPrivateKey, overrides: dict[str, Any]) -> None:
    with pytest.raises(InvalidToken):
        validator.validate(_token(key, **overrides))


def test_access_token_of_another_client_is_rejected(validator: TokenValidator, key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(InvalidToken, match="another app client"):
        validator.validate(_token(key, aud=None, token_use="access", client_id="other"), token_use="access")


def test_wrong_signature_is_rejected(validator: TokenValidator) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken):
        validator.validate(_token(other))


def test_unknown_key_is_refetched_once_then_rejected(validator: TokenValidator, key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(InvalidToken, match="unknown signing key"):
        validator.validate(_token(key, kid="rotated"))
    assert len(validator.fetched) == 1  # type: ignore[attr-defined]


def test_unsigned_token_is_rejected(validator: TokenValidator) -> None:
    token = jwt.encode({"iss": ISSUER, "sub": "x"}, key=None, algorithm="none")
    with pytest.raises(InvalidToken, match="algorithm"):
        validator.validate(token)


def test_garbage_is_rejected(validator: TokenValidator) -> None:
    with pytest.raises(InvalidToken, match="malformed"):
        validator.validate("not-a-token")
