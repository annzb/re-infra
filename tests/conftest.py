from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from rc_infra.catalog import Catalog, parse_catalog
from tests.fakes import FakeAws

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _run_from_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # Templates and the catalog are resolved relative to the repository root, like in CI.
    monkeypatch.chdir(REPO_ROOT)


@pytest.fixture
def raw_catalog() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load((REPO_ROOT / "environments/catalog.yaml").read_text()))


@pytest.fixture
def catalog(raw_catalog: dict[str, Any]) -> Catalog:
    return parse_catalog(raw_catalog)


@pytest.fixture
def fake() -> FakeAws:
    return FakeAws()
