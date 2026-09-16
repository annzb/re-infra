from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from rc_infra.env_config import EnvConfig, parse_env_config
from tests.fakes import FakeAws

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _run_from_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # Templates and envs.yaml are resolved relative to the repository root, like in CI.
    monkeypatch.chdir(REPO_ROOT)


@pytest.fixture
def raw_config() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load((REPO_ROOT / "envs.yaml").read_text()))


@pytest.fixture
def env_config(raw_config: dict[str, Any]) -> EnvConfig:
    return parse_env_config(raw_config)


@pytest.fixture
def fake() -> FakeAws:
    return FakeAws()
