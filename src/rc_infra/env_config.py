"""Load and validate envs.yaml, and derive every resource name from it.

The config is the only place environment names are declared. Everything else --
stack names, bucket names, table prefixes, SSM paths -- is derived here so that
planning, applying, and tearing down can never disagree about what belongs to an
environment.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_ENVS_PATH = Path("envs.yaml")
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

# These environments can never be removed from the config or torn down.
PROTECTED_ENVIRONMENTS = frozenset({"prod", "staging", "dev"})

# No hyphens: "rc-<env>-" must be an unambiguous prefix (rc-preview1- vs rc-preview1-x-),
# and 20 characters keeps the longest bucket name well under S3's 63-character limit.
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]{1,19}$")

DEFAULT_IDENTITY_PROFILE = "preview"

# Bucket purpose -> logical ID in infra/environment.yaml.
BUCKET_LOGICAL_IDS: dict[str, str] = {
    "embeddings": "EmbeddingsBucket",
    "user-corpus": "UserCorpusBucket",
    "avatars": "AvatarsBucket",
    "recordings": "RecordingsBucket",
    "schema-dumps": "SchemaDumpsBucket",
    "property-registry": "PropertyRegistryBucket",
}

RESOURCE_PREFIX = "rc"
CORE_STACK_PREFIX = f"{RESOURCE_PREFIX}-env-"
APP_STACK_PREFIX = f"{RESOURCE_PREFIX}-app-"
PLATFORM_STACK_NAME = f"{RESOURCE_PREFIX}-platform"


class EnvConfigError(Exception):
    """The config cannot be loaded or violates a rule. Holds every problem found."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IdentityProfile(_Strict):
    user_pool_id: str = Field(pattern=r"^[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]+$")
    client_id: str = Field(min_length=1)
    domain: str = Field(min_length=1)


class EnvironmentSpec(_Strict):
    identity: str | None = None


class EnvConfigFile(_Strict):
    schema_version: int
    account_id: str = Field(pattern=r"^\d{12}$")
    region: str = Field(pattern=r"^[a-z]{2}-[a-z]+-\d$")
    identity_profiles: dict[str, IdentityProfile]
    environments: dict[str, EnvironmentSpec]

    @field_validator("environments", mode="before")
    @classmethod
    def _empty_entries_are_defaults(cls, value: Any) -> Any:
        # "preview3:" with nothing after it parses as None; treat it like "preview3: {}".
        if isinstance(value, dict):
            return {name: {} if spec is None else spec for name, spec in value.items()}
        return value


@dataclass(frozen=True)
class Environment:
    name: str
    account_id: str
    region: str
    identity_profile: str
    identity: IdentityProfile

    @property
    def protected(self) -> bool:
        return self.name in PROTECTED_ENVIRONMENTS

    @property
    def core_stack(self) -> str:
        return core_stack_name(self.name)

    @property
    def app_stack(self) -> str:
        return f"{APP_STACK_PREFIX}{self.name}"

    @property
    def table_prefix(self) -> str:
        return table_prefix(self.name)

    @property
    def buckets(self) -> dict[str, str]:
        """Bucket purpose -> bucket name."""
        return {purpose: f"{RESOURCE_PREFIX}-{self.name}-{purpose}-{self.account_id}" for purpose in BUCKET_LOGICAL_IDS}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protected": self.protected,
            "account_id": self.account_id,
            "region": self.region,
            "core_stack": self.core_stack,
            "app_stack": self.app_stack,
            "table_prefix": self.table_prefix,
            "buckets": self.buckets,
            "identity_profile": self.identity_profile,
            "identity": self.identity.model_dump(),
        }


@dataclass(frozen=True)
class EnvConfig:
    account_id: str
    region: str
    environments: tuple[Environment, ...]
    # Kept whole, not just per environment: the identity resources belong to the
    # platform stack, which has no environment to resolve them through, and their
    # IDs are how an import identifies the pools AWS already assigned.
    identity_profiles: Mapping[str, IdentityProfile] = field(default_factory=dict)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(env.name for env in self.environments)

    def get(self, name: str) -> Environment:
        for env in self.environments:
            if env.name == name:
                return env
        raise KeyError(name)

    def to_json(self) -> str:
        return json.dumps(
            {
                "account_id": self.account_id,
                "region": self.region,
                "environments": [env.to_dict() for env in self.environments],
            },
            indent=2,
            sort_keys=True,
        )


def core_stack_name(environment: str) -> str:
    return f"{CORE_STACK_PREFIX}{environment}"


def table_prefix(environment: str) -> str:
    return f"{RESOURCE_PREFIX}-{environment}-"


def environment_from_core_stack(stack_name: str) -> str | None:
    """The environment a core stack name belongs to, or None if it is not one."""
    if not stack_name.startswith(CORE_STACK_PREFIX):
        return None
    name = stack_name.removeprefix(CORE_STACK_PREFIX)
    return name if ENVIRONMENT_NAME_PATTERN.match(name) else None


def load_env_config(path: Path = DEFAULT_ENVS_PATH) -> EnvConfig:
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise EnvConfigError([f"{path}: cannot read config: {exc}"]) from exc
    except yaml.YAMLError as exc:
        raise EnvConfigError([f"{path}: invalid YAML: {exc}"]) from exc
    return parse_env_config(raw)


def parse_env_config(raw: Any) -> EnvConfig:
    if not isinstance(raw, dict):
        raise EnvConfigError(["config: expected a mapping at the top level"])

    try:
        parsed = EnvConfigFile.model_validate(raw)
    except ValidationError as exc:
        raise EnvConfigError([_format_validation_error(err) for err in exc.errors()]) from exc

    errors: list[str] = []
    if parsed.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"schema_version: unsupported version {parsed.schema_version}; supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}")

    for name in sorted(PROTECTED_ENVIRONMENTS - parsed.environments.keys()):
        errors.append(f"environments.{name}: protected environment must not be removed")

    environments: list[Environment] = []
    for name, spec in sorted(parsed.environments.items()):
        if not ENVIRONMENT_NAME_PATTERN.match(name):
            errors.append(
                f"environments.{name}: name must match {ENVIRONMENT_NAME_PATTERN.pattern} "
                "(lowercase letters and digits, starting with a letter, at most 20 characters)"
            )
            continue

        profile_name = spec.identity or _default_identity_profile(name, parsed.identity_profiles)
        profile = parsed.identity_profiles.get(profile_name)
        if profile is None:
            errors.append(f"environments.{name}.identity: unknown identity profile {profile_name!r}; defined: {sorted(parsed.identity_profiles)}")
            continue

        environments.append(
            Environment(
                name=name,
                account_id=parsed.account_id,
                region=parsed.region,
                identity_profile=profile_name,
                identity=profile,
            )
        )

    if errors:
        raise EnvConfigError(errors)

    return EnvConfig(
        account_id=parsed.account_id,
        region=parsed.region,
        environments=tuple(environments),
        identity_profiles=dict(parsed.identity_profiles),
    )


def _default_identity_profile(name: str, profiles: dict[str, IdentityProfile]) -> str:
    return name if name in profiles else DEFAULT_IDENTITY_PROFILE


def _format_validation_error(error: Any) -> str:
    location = ".".join(str(part) for part in error["loc"]) or "config"
    return f"{location}: {error['msg']}"
