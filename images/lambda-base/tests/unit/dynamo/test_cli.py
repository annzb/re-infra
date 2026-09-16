"""Argument validation and exit codes for rc-dynamo-sync and rc-dynamo-report."""

from __future__ import annotations

from typing import Any

import pytest

from rc_lambda_base.dynamo import cli
from rc_lambda_base.dynamo.schema_report import SchemaReportError
from rc_lambda_base.dynamo.schema_sync import SchemaSyncError

MODULE = "acme.schema"


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DYNAMO_SCHEMA_MODULE",
        "DYNAMO_SCHEMA_TABLES",
        "DYNAMO_SCHEMA_DUMP_BUCKET",
        "RC_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the moving parts so the tests exercise the CLI, not DynamoDB."""
    calls: dict = {"sync": [], "report": []}

    def fake_sync(tables: Any, **kwargs: Any) -> bool:
        calls["sync"].append(kwargs)
        return calls.get("any_diff", False)

    def fake_report(tables: Any, **kwargs: Any) -> tuple:
        calls["report"].append(kwargs)
        return "REPORT", calls.get("any_findings", False)

    monkeypatch.setattr(cli, "load_schema_tables", lambda module_name: {"users": object()})
    monkeypatch.setattr(cli, "select_tables", lambda tables, selector, module_name: tables)
    monkeypatch.setattr(cli, "sync_tables", fake_sync)
    monkeypatch.setattr(cli, "build_report", fake_report)
    return calls


def _usage_error(argv: list[str], main: Any = cli.sync_main) -> int:
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert isinstance(caught.value.code, int)
    return caught.value.code


# ── rc-dynamo-sync ──────────────────────────────────────────────────


def test_schema_module_is_required(stub: dict) -> None:
    assert _usage_error([]) == 2


def test_schema_module_falls_back_to_env(stub: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DYNAMO_SCHEMA_MODULE", MODULE)
    # The default is read when the parser is built, so build it under the patched env.
    assert cli.sync_main([]) == cli.EXIT_OK


def test_apply_requires_an_environment(stub: dict) -> None:
    assert _usage_error(["--schema-module", MODULE, "--apply"]) == 2


def test_tag_requires_an_environment(stub: dict) -> None:
    assert _usage_error(["--schema-module", MODULE, "--tag", "Repository=core"]) == 2


def test_malformed_tag_is_a_usage_error(stub: dict) -> None:
    assert (
        _usage_error(["--schema-module", MODULE, "--environment", "dev", "--tag", "nonsense"]) == 2
    )


def test_tag_clashing_with_an_ownership_tag_is_rejected(stub: dict) -> None:
    assert (
        _usage_error(["--schema-module", MODULE, "--environment", "dev", "--tag", "ManagedBy=me"])
        == 2
    )


def test_apply_passes_ownership_tags(stub: dict) -> None:
    assert (
        cli.sync_main(
            [
                "--schema-module",
                MODULE,
                "--apply",
                "--environment",
                "preview3",
                "--tag",
                "Repository=retribalize-core",
            ]
        )
        == cli.EXIT_OK
    )
    tags = stub["sync"][0]["tags"]
    assert tags["ManagedBy"] == "rc-dynamo-sync"
    assert tags["LifecycleOwner"] == "rc-dynamo-sync"
    assert tags["Environment"] == "preview3"
    assert tags["Repository"] == "retribalize-core"
    assert stub["sync"][0]["apply"] is True


def test_dry_run_is_the_default(stub: dict) -> None:
    assert cli.sync_main(["--schema-module", MODULE]) == cli.EXIT_OK
    assert stub["sync"][0]["apply"] is False
    assert stub["sync"][0]["tags"] is None


def test_pending_changes_in_a_dry_run_exit_3(stub: dict) -> None:
    stub["any_diff"] = True
    assert cli.sync_main(["--schema-module", MODULE]) == cli.EXIT_CHANGES


def test_pending_changes_with_apply_exit_0(stub: dict) -> None:
    stub["any_diff"] = True
    assert (
        cli.sync_main(["--schema-module", MODULE, "--apply", "--environment", "dev"]) == cli.EXIT_OK
    )


def test_dump_bucket_comes_from_the_flag_or_env(
    stub: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.sync_main(["--schema-module", MODULE, "--dump-bucket", "rc-dev-schema-dumps"]) == 0
    assert stub["sync"][0]["dump_bucket"] == "rc-dev-schema-dumps"
    monkeypatch.setenv("DYNAMO_SCHEMA_DUMP_BUCKET", "from-env")
    assert cli.sync_main(["--schema-module", MODULE]) == 0
    assert stub["sync"][1]["dump_bucket"] == "from-env"


def test_schema_sync_error_exits_1(
    stub: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_: Any, **__: Any) -> bool:
        raise SchemaSyncError("dump bucket does not exist")

    monkeypatch.setattr(cli, "sync_tables", boom)
    assert cli.sync_main(["--schema-module", MODULE]) == cli.EXIT_ERROR
    assert "dump bucket does not exist" in capsys.readouterr().err


# ── rc-dynamo-report ────────────────────────────────────────────────


def test_report_requires_a_schema_module(stub: dict) -> None:
    assert _usage_error([], cli.report_main) == 2


def test_report_rejects_a_negative_sample(stub: dict) -> None:
    assert _usage_error(["--schema-module", MODULE, "--sample-items", "-1"], cli.report_main) == 2


def test_large_sample_needs_the_opt_in(stub: dict, capsys: pytest.CaptureFixture[str]) -> None:
    oversized = str(cli.MAX_SAMPLE_WITHOUT_OPT_IN + 1)
    assert (
        cli.report_main(["--schema-module", MODULE, "--sample-items", oversized]) == cli.EXIT_ERROR
    )
    assert (
        cli.report_main(
            ["--schema-module", MODULE, "--sample-items", oversized, "--allow-large-sample"]
        )
        == cli.EXIT_OK
    )


def test_report_findings_exit_3(stub: dict, capsys: pytest.CaptureFixture[str]) -> None:
    stub["any_findings"] = True
    assert cli.report_main(["--schema-module", MODULE]) == cli.EXIT_CHANGES
    assert "REPORT" in capsys.readouterr().out


def test_report_error_exits_1(
    stub: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_: Any, **__: Any) -> tuple:
        raise SchemaReportError("bad format")

    monkeypatch.setattr(cli, "build_report", boom)
    assert cli.report_main(["--schema-module", MODULE]) == cli.EXIT_ERROR
    assert "bad format" in capsys.readouterr().err
