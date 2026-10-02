from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rc_infra import aws as aws_module
from rc_infra.cli import main
from tests.fakes import ACCOUNT_ID, FakeAws, env_stack_tags


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch, fake: FakeAws) -> FakeAws:
    monkeypatch.setattr(aws_module, "caller_account", lambda region: ACCOUNT_ID)
    monkeypatch.setattr(aws_module, "connect", lambda region: fake.aws)
    return fake


def test_validate(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate"]) == 0
    assert "is valid: 13 environments" in capsys.readouterr().out


def test_validate_invalid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "env_config.yaml"
    path.write_text("schema_version: 1\nenvironments: {}\n")
    assert main(["validate", "--config", str(path)]) == 1
    assert "is invalid" in capsys.readouterr().err


def test_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["frobnicate"])
    assert caught.value.code == 2


def test_blocked_plan_is_never_applied(connected: FakeAws) -> None:
    connected.stacks.add("rc-env-dev", status="UPDATE_ROLLBACK_FAILED", tags=env_stack_tags("dev"))
    assert main(["apply"]) == 1
    assert main(["apply", "--yes"]) == 1
    assert connected.stacks.deploys == []


def test_wrong_account_is_refused(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail_connect(*_: Any) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr(aws_module, "caller_account", lambda region: "111111111111")
    monkeypatch.setattr(aws_module, "connect", fail_connect)
    assert main(["apply", "--yes"]) == 1
    assert "account 111111111111" in capsys.readouterr().err


def test_apply_without_yes_is_a_dry_run(connected: FakeAws) -> None:
    assert main(["apply"]) == 0
    assert connected.stacks.deploys == []


def test_apply_with_yes(connected: FakeAws) -> None:
    assert main(["apply", "--yes"]) == 0
    assert connected.stacks.deploys


def test_apply_prints_data_status(connected: FakeAws, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["apply", "--yes"]) == 0
    assert "DATA STATUS" in capsys.readouterr().out


def test_status(connected: FakeAws, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--environment", "dev"]) == 0
    out = capsys.readouterr().out
    assert "dev / EmbeddingsBucket" in out
    assert "prod /" not in out


def test_status_json(connected: FakeAws, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--format", "json"]) == 0
    assert {row["state"] for row in json.loads(capsys.readouterr().out)} == {"NOT CREATED"}


def test_status_unknown_environment_is_a_usage_error(connected: FakeAws) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["status", "--environment", "nope"])
    assert caught.value.code == 2


def test_outputs_without_stacks_fails(connected: FakeAws, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["outputs", "--environment", "dev"]) == 1
    assert "rc-platform does not exist" in capsys.readouterr().err
