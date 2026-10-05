"""Validate Cognito tokens strictly. The replacement for core's layer/cognito_jwt.py.

Every check is mandatory: signature (RS256, key from the pool's JWKS), issuer,
expiry, token_use, and the app client (`aud` for ID tokens, `client_id` for access
tokens). A token that fails any of them is rejected; nothing falls back to reading
unverified claims. Profile provisioning and other application logic do not belong here.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Callable
from typing import Any, Literal

import jwt
from jwt.algorithms import RSAAlgorithm

TokenUse = Literal["id", "access"]
JwksFetcher = Callable[[str], dict[str, Any]]


class InvalidToken(Exception):
    pass


def fetch_jwks(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return dict(json.load(response))


class TokenValidator:
    def __init__(self, issuer: str, client_id: str, *, fetch: JwksFetcher = fetch_jwks, leeway_seconds: int = 0) -> None:
        if not issuer.startswith("https://cognito-idp.") or not client_id:
            raise ValueError("issuer must be a Cognito user pool issuer URL and client_id must be set")
        self.issuer = issuer
        self.client_id = client_id
        self._fetch = fetch
        self._leeway = leeway_seconds
        self._keys: dict[str, Any] = {}
        self._lock = threading.Lock()

    def validate(self, token: str, token_use: TokenUse = "id") -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken(f"malformed token: {exc}") from exc
        if header.get("alg") != "RS256":
            raise InvalidToken(f"unexpected algorithm {header.get('alg')!r}")
        key = self._key(str(header.get("kid", "")))
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.client_id if token_use == "id" else None,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "sub", "token_use"], "verify_aud": token_use == "id"},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        if claims.get("token_use") != token_use:
            raise InvalidToken(f"expected a {token_use} token, got {claims.get('token_use')!r}")
        if token_use == "access" and claims.get("client_id") != self.client_id:
            raise InvalidToken("access token was issued to another app client")
        return claims

    def _key(self, kid: str) -> Any:
        with self._lock:
            if kid not in self._keys:
                # An unknown key ID means the pool rotated its keys: refetch once.
                jwks = self._fetch(f"{self.issuer}/.well-known/jwks.json")
                self._keys = {str(k["kid"]): RSAAlgorithm.from_jwk(json.dumps(k)) for k in jwks.get("keys", [])}
            if kid not in self._keys:
                raise InvalidToken(f"unknown signing key {kid!r}")
            return self._keys[kid]
