"""CLI selection, exit-code, and guard behaviors."""

from __future__ import annotations

import pytest

from . import dynamo_helpers as dh
from . import schema_cases as sc
from . import smoke_cases as smc
from .cli_helpers import output, run_schema_sync


def test_unknown_table_name_fails_clearly():
    result = run_schema_sync(tables="does_not_exist")

    assert result.returncode != 0, output(result)
    assert "Unknown table(s)" in (result.stdout + result.stderr), output(result)


def test_tables_all_selects_only_basetable_instances_from_schema_module(managed_table):
    name = managed_table(smc.all_smoke_table)
    smoke_env = {"DYNAMO_SCHEMA_MODULE": "tests.integration.dynamo_schema_sync.smoke_cases"}

    result = run_schema_sync(tables="all", apply=True, extra_env=smoke_env)

    assert result.returncode == 0, output(result)
    # Only the single BaseTable instance is created; NOT_A_TABLE is ignored.
    assert dh.table_exists(name)


def test_dry_run_diff_returns_3_apply_success_returns_0(managed_table):
    managed_table(sc.create_simple_table)

    assert run_schema_sync(tables="create_simple_table").returncode == 3
    assert run_schema_sync(tables="create_simple_table", apply=True).returncode == 0
    assert run_schema_sync(tables="create_simple_table").returncode == 0


def test_recreate_apply_requires_dump_bucket(managed_table):
    name = managed_table(sc.recreate_pk_changed_table)
    dh.create_live_table(name, {"partition_key": "old_pk", "sort_key": None})
    dh.put_items(name, [{"old_pk": "1", "pk": "a"}])

    # Recreate is allowed but no dump bucket is given. There is no fallback
    # bucket, so the guard must refuse rather than drop the table.
    result = run_schema_sync(
        tables="recreate_pk_changed_table",
        apply=True,
        dump_bucket="",
        extra_env={"DYNAMO_ALLOW_TABLE_RECREATE": "true", "DYNAMO_SCHEMA_DUMP_BUCKET": ""},
    )

    assert result.returncode != 0, output(result)
    combined = (result.stdout + result.stderr).lower()
    assert "dump" in combined and "required" in combined, output(result)
    # Original table remains untouched.
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "old_pk", "sort_key": None}
    assert len(dh.scan_items(name)) == 1


def test_missing_dump_bucket_is_rejected(managed_table):
    name = managed_table(sc.recreate_sk_removed_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": "old_sk"})
    dh.put_items(name, [{"pk": "a", "old_sk": "1"}])

    missing_bucket = f"{sc.PREFIX}-{sc.RUN_ID}-absent-dumps"
    dh.delete_bucket(missing_bucket)  # ensure it does not exist
    try:
        result = run_schema_sync(
            tables="recreate_sk_removed_table",
            apply=True,
            extra_env={
                "DYNAMO_ALLOW_TABLE_RECREATE": "true",
                "DYNAMO_SCHEMA_DUMP_BUCKET": missing_bucket,
            },
        )

        assert result.returncode != 0, output(result)
        assert not dh.bucket_exists(missing_bucket), "the dump bucket must never be created here"
        # The table and its data survive the refusal.
        assert dh.describe_schema(name)["key_schema"] == {
            "partition_key": "pk",
            "sort_key": "old_sk",
        }
        assert len(dh.scan_items(name)) == 1
    finally:
        dh.delete_bucket(missing_bucket)


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
