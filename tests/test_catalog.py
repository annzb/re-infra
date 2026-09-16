from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from rc_infra.catalog import (
    PROTECTED_ENVIRONMENTS,
    CatalogError,
    environment_from_core_stack,
    load_catalog,
    parse_catalog,
)


def _errors(raw: Any) -> list[str]:
    with pytest.raises(CatalogError) as caught:
        parse_catalog(raw)
    return caught.value.errors


def test_repository_catalog_is_valid() -> None:
    catalog = load_catalog()
    assert catalog.names >= PROTECTED_ENVIRONMENTS
    assert {f"preview{n}" for n in (*range(1, 9), 67, 89)} <= catalog.names


def test_derived_names(catalog: Any) -> None:
    env = catalog.get("preview67")
    assert env.core_stack == "rc-env-preview67"
    assert env.app_stack == "rc-app-preview67"
    assert env.table_prefix == "rc-preview67-"
    assert env.ssm_prefix == "/rc/env/preview67/"
    assert env.buckets["user-corpus"] == "rc-preview67-user-corpus-273268178059"
    assert not env.protected
    assert catalog.get("prod").protected


def test_identity_defaults_to_same_named_profile_then_preview(catalog: Any) -> None:
    assert catalog.get("staging").identity_profile == "staging"
    assert catalog.get("preview3").identity_profile == "preview"
    assert catalog.get("preview3").identity.user_pool_id == "us-east-1_LwH5lWQ0q"


def test_explicit_identity(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["environments"]["preview3"] = {"identity": "dev"}
    assert parse_catalog(raw_catalog).get("preview3").identity_profile == "dev"


def test_unknown_identity(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["environments"]["preview3"] = {"identity": "nope"}
    assert any("environments.preview3.identity" in e for e in _errors(raw_catalog))


def test_empty_entry_means_defaults(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["environments"]["preview9"] = None
    assert "preview9" in parse_catalog(raw_catalog).names


@pytest.mark.parametrize("name", ["Preview9", "preview-9", "9preview", "p", "a" * 21])
def test_bad_environment_names(raw_catalog: dict[str, Any], name: str) -> None:
    raw_catalog["environments"][name] = {}
    assert any(f"environments.{name}: name must match" in e for e in _errors(raw_catalog))


def test_unknown_environment_field_reports_path(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["environments"]["preview3"] = {"idenity": "dev"}
    assert any(e.startswith("environments.preview3.idenity:") for e in _errors(raw_catalog))


def test_unknown_top_level_field(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["regoin"] = "us-west-2"
    assert any(e.startswith("regoin:") for e in _errors(raw_catalog))


@pytest.mark.parametrize("name", sorted(PROTECTED_ENVIRONMENTS))
def test_protected_environment_cannot_be_removed(raw_catalog: dict[str, Any], name: str) -> None:
    del raw_catalog["environments"][name]
    assert f"environments.{name}: protected environment must not be removed" in _errors(raw_catalog)


def test_unsupported_schema_version(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["schema_version"] = 2
    assert any(e.startswith("schema_version: unsupported") for e in _errors(raw_catalog))


def test_invalid_account_id(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["account_id"] = "1234"
    assert any(e.startswith("account_id:") for e in _errors(raw_catalog))


def test_top_level_must_be_mapping() -> None:
    assert _errors(["prod"]) == ["catalog: expected a mapping at the top level"]


def test_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text("environments: {prod: [unclosed\n")
    with pytest.raises(CatalogError) as caught:
        load_catalog(path)
    assert "invalid YAML" in caught.value.errors[0]


def test_rendering_is_deterministic(raw_catalog: dict[str, Any]) -> None:
    shuffled = dict(raw_catalog)
    items = list(raw_catalog["environments"].items())
    random.Random(7).shuffle(items)
    shuffled["environments"] = dict(items)
    assert parse_catalog(shuffled).to_json() == parse_catalog(raw_catalog).to_json()


def test_longest_name_fits_bucket_limit(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["environments"]["a" * 20] = {}
    env = parse_catalog(raw_catalog).get("a" * 20)
    assert max(len(name) for name in env.buckets.values()) <= 63


@pytest.mark.parametrize(
    ("stack", "expected"),
    [
        ("rc-env-preview3", "preview3"),
        ("rc-env-", None),
        ("rc-envx", None),
        ("rc-app-prod", None),
        ("rc-env-Bad-Name", None),
    ],
)
def test_environment_from_core_stack(stack: str, expected: str | None) -> None:
    assert environment_from_core_stack(stack) == expected
