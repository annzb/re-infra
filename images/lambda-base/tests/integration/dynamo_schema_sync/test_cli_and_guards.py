"""CLI selection, exit-code, and guard behaviors."""
from __future__ import annotations

import pytest

from . import dynamo_helpers as dh
from . import schema_cases as sc
from . import smoke_cases as smc
from .cli_helpers import output, run_schema_sync

SMOKE_ENV = {"DYNAMO_SCHEMA_MODULE": "tests.integration.dynamo_schema_sync.smoke_cases"}


def test_unknown_table_name_fails_clearly():
    result = run_schema_sync(tables="does_not_exist")

    assert result.returncode == 1, output(result)
    assert "Unknown table" in result.stderr, output(result)


def test_tables_all_selects_only_the_tables_registry(managed_table):
    name = managed_table(smc.all_smoke_table)
    unregistered = managed_table(smc.unregistered_smoke_table)

    result = run_schema_sync(tables="all", apply=True, extra_env=SMOKE_ENV)

    assert result.returncode == 0, output(result)
    assert dh.table_exists(name)
    # Declared in the module but not registered in TABLES: never touched.
    assert not dh.table_exists(unregistered)


def test_schema_module_without_tables_registry_fails():
    result = run_schema_sync(
        tables="all",
        extra_env={"DYNAMO_SCHEMA_MODULE": "tests.integration.dynamo_schema_sync.dynamo_helpers"},
    )

    assert result.returncode == 1, output(result)
    assert "does not export TABLES" in result.stderr, output(result)


def test_dry_run_diff_returns_3_apply_success_returns_0(managed_table):
    managed_table(sc.create_simple_table)

    assert run_schema_sync(tables="create_simple_table").returncode == 3
    assert run_schema_sync(tables="create_simple_table", apply=True).returncode == 0
    assert run_schema_sync(tables="create_simple_table").returncode == 0


def test_apply_requires_environment(managed_table):
    name = managed_table(sc.create_simple_table)

    result = run_schema_sync(tables="create_simple_table", apply=True, extra_env={"RC_ENVIRONMENT": ""})

    assert result.returncode == 2, output(result)
    assert "--environment" in result.stderr, output(result)
    assert not dh.table_exists(name)


def test_recreate_apply_requires_dump_bucket(managed_table):
    name = managed_table(sc.recreate_pk_changed_table)
    dh.create_live_table(name, {"partition_key": "old_pk", "sort_key": None})
    dh.put_items(name, [{"old_pk": "1", "pk": "a"}])

    # Enable recreate but provide no dump bucket: an explicit empty --dump-bucket
    # with DYNAMO_SCHEMA_DUMP_BUCKET also blanked. There is no fallback bucket.
    result = run_schema_sync(
        tables="recreate_pk_changed_table",
        apply=True,
        dump_bucket="",
        extra_env={"DYNAMO_ALLOW_TABLE_RECREATE": "true", "DYNAMO_SCHEMA_DUMP_BUCKET": ""},
    )

    assert result.returncode == 1, output(result)
    combined = (result.stdout + result.stderr).lower()
    assert "dump" in combined and "required" in combined, output(result)
    # Original table remains untouched.
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "old_pk", "sort_key": None}
    assert len(dh.scan_items(name)) == 1


def test_missing_dump_bucket_is_rejected_not_created(managed_table):
    name = managed_table(sc.recreate_sk_removed_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": "old_sk"})
    dh.put_items(name, [{"pk": "a", "old_sk": "1"}])

    missing_bucket = f"{sc.PREFIX}-{sc.RUN_ID}-missing-dumps"
    dh.delete_bucket(missing_bucket)  # ensure it does not exist

    result = run_schema_sync(
        tables="recreate_sk_removed_table",
        apply=True,
        extra_env={"DYNAMO_ALLOW_TABLE_RECREATE": "true", "DYNAMO_SCHEMA_DUMP_BUCKET": missing_bucket},
    )

    assert result.returncode == 1, output(result)
    assert "does not exist" in result.stderr, output(result)
    assert not dh.bucket_exists(missing_bucket)
    # Rejected before any change: the live table keeps its schema and rows.
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": "old_sk"}
    assert len(dh.scan_items(name)) == 1


@pytest.mark.parametrize("truthy", ["true", "1", "yes"])
def test_prune_undeclared_env_bool_accepts_true_values(managed_table, truthy):
    name = managed_table(sc.remote_only_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI-Extra": {"partition_key": "extra", "sort_key": None}},
    )

    result = run_schema_sync(
        tables="remote_only_gsi_table", apply=True, extra_env={"DYNAMO_PRUNE_UNDECLARED": truthy}
    )

    assert result.returncode == 0, output(result)
    assert "GSI-Extra" not in dh.describe_schema(name)["gsis"]


def test_invalid_boolean_flag_fails_instead_of_reading_as_false(managed_table):
    managed_table(sc.remote_only_gsi_table)

    result = run_schema_sync(tables="remote_only_gsi_table", extra_env={"DYNAMO_PRUNE_UNDECLARED": "maybe"})

    assert result.returncode == 1, output(result)
    assert "DYNAMO_PRUNE_UNDECLARED" in result.stderr, output(result)
