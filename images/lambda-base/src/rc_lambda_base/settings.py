"""Settings the base package reads from the environment.

Deliberately small: only what the generic DynamoDB framework needs. Application
settings stay in the application.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_REGION = "us-east-1"

_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off"})

# Renamed 2026-08-10. A stale value would otherwise go silently inert, possibly
# in the permissive direction.
LEGACY_ENV_VARS: Mapping[str, str] = {"DYNAMO_ALLOW_GSI_DELETE": "DYNAMO_PRUNE_UNDECLARED"}


class SettingsError(ValueError):
    """An environment variable holds a value the package cannot use."""


def _raw(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None:
        return None
    return value.strip() or None


def parse_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _raw(environ, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise SettingsError(
        f"{name}={raw!r} is not a boolean; use one of {sorted(_TRUE_VALUES | _FALSE_VALUES)}"
    )


def parse_positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = _raw(environ, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name}={raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise SettingsError(f"{name}={raw!r} must be a positive, finite number")
    return value


@dataclass(frozen=True)
class Settings:
    aws_region: str = DEFAULT_REGION
    aws_endpoint_url: str | None = None
    # Table/GSI polling interval. LocalStack converges near-instantly, so local
    # integration tests drop this to 1s.
    schema_poll_seconds: float = 10.0
    schema_wait_timeout_seconds: float = 3600.0
    # Destructive schema-sync permissions. Both stay off unless there is a reason.
    prune_undeclared: bool = False
    allow_table_recreate: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        stale = sorted(name for name in LEGACY_ENV_VARS if _raw(env, name))
        if stale:
            raise SettingsError(
                "Renamed environment variable(s) are still set: "
                + ", ".join(f"{name} (now {LEGACY_ENV_VARS[name]})" for name in stale)
                + ". Unset them so the value cannot be silently ignored."
            )

        return cls(
            aws_region=_raw(env, "AWS_REGION") or _raw(env, "AWS_DEFAULT_REGION") or DEFAULT_REGION,
            aws_endpoint_url=_raw(env, "AWS_ENDPOINT_URL"),
            schema_poll_seconds=parse_positive_float(env, "DYNAMO_SCHEMA_POLL_SECONDS", 10.0),
            schema_wait_timeout_seconds=parse_positive_float(
                env, "DYNAMO_SCHEMA_WAIT_TIMEOUT_SECONDS", 3600.0
            ),
            prune_undeclared=parse_bool(env, "DYNAMO_PRUNE_UNDECLARED", False),
            allow_table_recreate=parse_bool(env, "DYNAMO_ALLOW_TABLE_RECREATE", False),
        )
