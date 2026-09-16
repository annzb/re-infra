"""CLI argument validation, the TABLES contract, and exit codes (no AWS)."""
import sys
import types
from typing import ClassVar

import pytest

from rc_lambda_base.dynamo import BaseItem, BaseTable, cli
from rc_lambda_base.dynamo.schema_sync import SchemaSyncError

from .fakes import FakeDynamoTable

MODULE = "rc_test_schema_module"

ENV_VARS = (
    "DYNAMO_SCHEMA_MODULE",
    "DYNAMO_SCHEMA_TABLES",
    "DYNAMO_SCHEMA_DUMP_BUCKET",
    "RC_ENVIRONMENT",
    "DYNAMO_PRUNE_UNDECLARED",
    "DYNAMO_ALLOW_TABLE_RECREATE",
    "DYNAMO_ALLOW_GSI_DELETE",
)


class _Item(BaseItem):
    partition_key: ClassVar[str] = "pk"
    pk: str


class _Table(BaseTable[_Item]):
    table_name = "cli-things"
    item_model = _Item


def _table():
    return _Table(FakeDynamoTable(key_fields=("pk", None)))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _install_module(monkeypatch, **attributes):
    module = types.ModuleType(MODULE)
    for name, value in attributes.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, MODULE, module)
    return module


@pytest.fixture
def schema_module(monkeypatch):
    things, others = _table(), _table()
    return _install_module(monkeypatch, things=things, others=others, TABLES={"things": things, "others": others})


@pytest.fixture
def fake_sync(monkeypatch):
    state = {"calls": [], "any_diff": False, "error": None}

    def _sync(tables, **kwargs):
        state["calls"].append({"tables": list(tables), **kwargs})
        if state["error"]:
            raise SchemaSyncError(state["error"])
        return state["any_diff"]

    monkeypatch.setattr(cli, "sync_tables", _sync)
    return state


def _usage_error(main, argv, capsys) -> str:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    return capsys.readouterr().err


# ───────────────────────── rc-dynamo-sync: usage ─────────────────────────

def test_sync_requires_a_schema_module(capsys):
    assert "--schema-module" in _usage_error(cli.sync_main, [], capsys)


def test_sync_apply_requires_an_environment(schema_module, fake_sync, capsys):
    assert "--environment" in _usage_error(cli.sync_main, ["--schema-module", MODULE, "--apply"], capsys)
    assert fake_sync["calls"] == []


@pytest.mark.parametrize("tag", ["no-equals-sign", "=value"])
def test_sync_rejects_malformed_tags(schema_module, fake_sync, capsys, tag):
    argv = ["--schema-module", MODULE, "--environment", "dev", "--tag", tag]
    assert "KEY=VALUE" in _usage_error(cli.sync_main, argv, capsys)


def test_sync_rejects_overriding_ownership_tags(schema_module, fake_sync, capsys):
    argv = ["--schema-module", MODULE, "--environment", "dev", "--tag", "ManagedBy=me"]
    assert "cannot be overridden" in _usage_error(cli.sync_main, argv, capsys)


def test_sync_tag_requires_an_environment(schema_module, fake_sync, capsys):
    argv = ["--schema-module", MODULE, "--tag", "Repository=core"]
    assert "--tag requires --environment" in _usage_error(cli.sync_main, argv, capsys)


# ───────────────────────── rc-dynamo-sync: behavior ─────────────────────────

def test_sync_dry_run_is_the_default_and_exits_0_when_clean(monkeypatch, schema_module, fake_sync):
    monkeypatch.setenv("DYNAMO_SCHEMA_MODULE", MODULE)

    assert cli.sync_main([]) == 0

    (call,) = fake_sync["calls"]
    assert call["apply"] is False
    assert call["tags"] is None
    assert call["tables"] == [schema_module.things, schema_module.others]


def test_sync_dry_run_exits_3_when_changes_are_pending(schema_module, fake_sync):
    fake_sync["any_diff"] = True
    assert cli.sync_main(["--schema-module", MODULE]) == 3


def test_sync_apply_exits_0_and_passes_tags_and_dump_bucket(monkeypatch, schema_module, fake_sync):
    monkeypatch.setenv("RC_ENVIRONMENT", "preview3")
    monkeypatch.setenv("DYNAMO_SCHEMA_DUMP_BUCKET", "rc-preview3-schema-dumps-000000000000")
    fake_sync["any_diff"] = True

    exit_code = cli.sync_main([
        "--schema-module", MODULE, "--apply", "--tables", "others", "--tag", "Repository=retribalize-core",
    ])

    assert exit_code == 0
    (call,) = fake_sync["calls"]
    assert call["apply"] is True
    assert call["tables"] == [schema_module.others]
    assert call["dump_bucket"] == "rc-preview3-schema-dumps-000000000000"
    assert call["tags"] == {
        "ManagedBy": "rc-dynamo-sync",
        "LifecycleOwner": "rc-dynamo-sync",
        "Environment": "preview3",
        "Repository": "retribalize-core",
    }


def test_sync_errors_exit_1_with_the_message_on_stderr(schema_module, fake_sync, capsys):
    fake_sync["error"] = "Refusing to recreate things without permission."
    assert cli.sync_main(["--schema-module", MODULE]) == 1
    assert "Refusing to recreate" in capsys.readouterr().err


def test_sync_unknown_table_key_exits_1(schema_module, fake_sync, capsys):
    assert cli.sync_main(["--schema-module", MODULE, "--tables", "things,nope"]) == 1
    assert "Unknown table(s) ['nope']" in capsys.readouterr().err
    assert fake_sync["calls"] == []


def test_sync_invalid_settings_exit_1(monkeypatch, schema_module, fake_sync, capsys):
    monkeypatch.setenv("DYNAMO_ALLOW_GSI_DELETE", "true")
    assert cli.sync_main(["--schema-module", MODULE]) == 1
    assert "DYNAMO_ALLOW_GSI_DELETE" in capsys.readouterr().err


# ───────────────────────── schema module contract ─────────────────────────

def test_module_without_tables_registry_exits_1(monkeypatch, fake_sync, capsys):
    # A BaseTable instance alone is not enough: only TABLES is read.
    _install_module(monkeypatch, things=_table())
    assert cli.sync_main(["--schema-module", MODULE]) == 1
    assert "does not export TABLES" in capsys.readouterr().err


def test_tables_registry_must_be_a_mapping(monkeypatch, fake_sync, capsys):
    _install_module(monkeypatch, TABLES=[_table()])
    assert cli.sync_main(["--schema-module", MODULE]) == 1
    assert "must be a mapping" in capsys.readouterr().err


def test_tables_registry_entries_must_be_tables(monkeypatch, fake_sync, capsys):
    _install_module(monkeypatch, TABLES={"things": _table(), "constant": "not-a-table"})
    assert cli.sync_main(["--schema-module", MODULE]) == 1
    assert "['constant']" in capsys.readouterr().err


def test_unimportable_schema_module_exits_1(fake_sync, capsys):
    assert cli.sync_main(["--schema-module", "rc_no_such_module_anywhere"]) == 1
    assert "Cannot import schema module" in capsys.readouterr().err


# ───────────────────────── rc-dynamo-report ─────────────────────────

def test_report_requires_a_schema_module(capsys):
    assert "--schema-module" in _usage_error(cli.report_main, [], capsys)


def test_report_rejects_negative_sample_size(schema_module, capsys):
    assert "negative" in _usage_error(cli.report_main, ["--schema-module", MODULE, "--sample-items", "-1"], capsys)


def test_report_large_sample_requires_opt_in(schema_module, capsys):
    assert cli.report_main(["--schema-module", MODULE, "--sample-items", "99999"]) == 1
    assert "allow-large-sample" in capsys.readouterr().err


def test_report_exit_code_follows_findings(monkeypatch, schema_module, capsys):
    monkeypatch.setattr(cli, "build_report", lambda tables, **kwargs: ("things: drifted", True))
    assert cli.report_main(["--schema-module", MODULE]) == 3
    assert "things: drifted" in capsys.readouterr().out

    monkeypatch.setattr(cli, "build_report", lambda tables, **kwargs: ("things: OK", False))
    assert cli.report_main(["--schema-module", MODULE]) == 0
