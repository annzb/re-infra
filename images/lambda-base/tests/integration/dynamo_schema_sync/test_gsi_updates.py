"""GSI updates that do NOT require a table recreate."""

from __future__ import annotations

import pytest

from . import dynamo_helpers as dh
from . import schema_cases as sc
from .cli_helpers import output, run_schema_sync

PRUNE_ENV = {"DYNAMO_PRUNE_UNDECLARED": "true"}

# Strings printed only on the table-recreate path (_delete_table / _dump_table_to_s3).
# Asserting their absence is what proves an index was repaired in place. Note that
# an empty dump-object listing does NOT prove it: a successful recreate deletes its
# own dump, so the listing is empty either way.
RECREATE_MARKERS = ("[schema-sync] Deleting table:", "[schema-sync] Dumping ")


def assert_no_table_recreate(result) -> None:
    for marker in RECREATE_MARKERS:
        assert marker not in result.stdout, output(result)


def test_apply_adds_missing_gsi_without_recreate(managed_table, dump_bucket):
    name = managed_table(sc.missing_gsi_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": None})
    dh.put_items(name, [{"pk": "a", "gsi_a": "x"}, {"pk": "b", "gsi_a": "y"}])
    dumps_before = dh.list_dump_objects(dump_bucket, name)

    result = run_schema_sync(tables="missing_gsi_table", apply=True)

    assert result.returncode == 0, output(result)
    assert "GSI-A" in dh.describe_schema(name)["gsis"]
    assert {i["pk"] for i in dh.scan_items(name)} == {"a", "b"}
    # No recreate -> the S3 dump path is never touched.
    assert dh.list_dump_objects(dump_bucket, name) == dumps_before


def test_remote_only_gsi_is_ignored_when_delete_disabled(managed_table):
    name = managed_table(sc.remote_only_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI-Extra": {"partition_key": "extra", "sort_key": None}},
    )
    dh.put_items(name, [{"pk": "a", "extra": "x"}])

    dry = run_schema_sync(tables="remote_only_gsi_table")
    # Exit 0, not 3: an index nobody declared is not something the deploy will
    # change, so it must not read as pending work.
    assert dry.returncode == 0, output(dry)
    assert "GSI-Extra" in dry.stdout, output(dry)
    assert "not declared in Python" in dry.stdout, output(dry)

    apply = run_schema_sync(tables="remote_only_gsi_table", apply=True)
    assert apply.returncode == 0, output(apply)
    assert "GSI-Extra" in dh.describe_schema(name)["gsis"]
    assert {i["pk"] for i in dh.scan_items(name)} == {"a"}


def test_remote_only_gsi_is_deleted_when_delete_enabled(managed_table):
    name = managed_table(sc.remote_only_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI-Extra": {"partition_key": "extra", "sort_key": None}},
    )
    dh.put_items(name, [{"pk": "a", "extra": "x"}])
    delete_env = dict(PRUNE_ENV)

    apply = run_schema_sync(tables="remote_only_gsi_table", apply=True, extra_env=delete_env)
    assert apply.returncode == 0, output(apply)
    assert "GSI-Extra" not in dh.describe_schema(name)["gsis"]
    assert {i["pk"] for i in dh.scan_items(name)} == {"a"}

    second = run_schema_sync(tables="remote_only_gsi_table", extra_env=delete_env)
    assert second.returncode == 0, output(second)


def test_missing_and_remote_only_gsis_reconciled_when_delete_enabled(managed_table):
    name = managed_table(sc.reconcile_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI-Obsolete": {"partition_key": "obsolete", "sort_key": None}},
    )
    dh.put_items(name, [{"pk": "a", "gsi_a": "x", "obsolete": "o"}])

    apply = run_schema_sync(tables="reconcile_gsi_table", apply=True, extra_env=PRUNE_ENV)
    assert apply.returncode == 0, output(apply)
    assert set(dh.describe_schema(name)["gsis"]) == {"GSI-A"}
    assert {i["pk"] for i in dh.scan_items(name)} == {"a"}


def test_live_composite_gsi_sort_key_is_ignored_when_expected_sort_key_unmodeled(
    managed_table, dump_bucket
):
    # Regression: a pre-existing composite GSI must not be treated as drift when
    # the expected schema cannot model the sort key (IGNORE_UNDECLARED_GSI_SORT_KEYS).
    name = managed_table(sc.ignore_composite_remote_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI1-Test": {"partition_key": "gsi_pk", "sort_key": "gsi_sk"}},
        attribute_types={"gsi_pk": "S", "gsi_sk": "S"},
    )
    dh.put_items(name, [{"pk": "a", "gsi_pk": "g", "gsi_sk": "s"}])

    dry = run_schema_sync(tables="ignore_composite_remote_gsi_table")
    assert dry.returncode == 0, output(dry)

    apply = run_schema_sync(tables="ignore_composite_remote_gsi_table", apply=True)
    assert apply.returncode == 0, output(apply)
    # Surfaced so it can be adopted, but it does not count as pending work.
    assert "does not model" in dry.stdout, output(dry)

    # Live composite GSI is untouched; no recreate happened.
    assert dh.describe_schema(name)["gsis"]["GSI1-Test"] == {
        "partition_key": "gsi_pk",
        "sort_key": "gsi_sk",
    }
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_changed_gsi_partition_key_rebuilt_in_place_ungated(managed_table, dump_bucket):
    name = managed_table(sc.changed_gsi_pk_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI1-Test": {"partition_key": "old_gsi_pk", "sort_key": None}},
    )
    dh.put_items(name, [{"pk": "a", "gsi_pk": "g", "old_gsi_pk": "o"}])

    apply = run_schema_sync(tables="changed_gsi_pk_table", apply=True)
    assert apply.returncode == 0, output(apply)
    assert dh.describe_schema(name)["gsis"]["GSI1-Test"] == {
        "partition_key": "gsi_pk",
        "sort_key": None,
    }

    # Repaired by rebuilding the index, not by recreating the table: rows keep
    # even the attributes that are not model fields.
    assert_no_table_recreate(apply)
    items = {i["pk"]: i for i in dh.scan_items(name)}
    assert set(items) == {"a"}
    assert items["a"]["old_gsi_pk"] == "o"
    assert dh.list_dump_objects(dump_bucket, name) == []

    # Convergence: a second dry run sees no drift. This is the direct assertion
    # against "--apply exits 0 having repaired nothing".
    second = run_schema_sync(tables="changed_gsi_pk_table")
    assert second.returncode == 0, output(second)


def test_changed_gsi_sort_key_rebuilt_in_place_ungated(managed_table, dump_bucket):
    name = managed_table(sc.changed_gsi_sk_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI1-Test": {"partition_key": "gsi_pk", "sort_key": None}},
    )
    dh.put_items(name, [{"pk": "a", "gsi_pk": "g", "gsi_sk": "s"}])

    apply = run_schema_sync(tables="changed_gsi_sk_table", apply=True)
    assert apply.returncode == 0, output(apply)
    assert dh.describe_schema(name)["gsis"]["GSI1-Test"] == {
        "partition_key": "gsi_pk",
        "sort_key": "gsi_sk",
    }

    assert_no_table_recreate(apply)
    assert {i["pk"] for i in dh.scan_items(name)} == {"a"}
    assert dh.list_dump_objects(dump_bucket, name) == []

    second = run_schema_sync(tables="changed_gsi_sk_table")
    assert second.returncode == 0, output(second)


def test_changed_missing_and_remote_only_gsis_reconciled_in_one_apply(managed_table, dump_bucket):
    # Ordering test: remote-only delete, changed rebuild and missing create must
    # all succeed in a single run without tripping DynamoDB's index constraints.
    name = managed_table(sc.reconcile_all_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={
            "GSI-A": {"partition_key": "wrong_a", "sort_key": None},  # changed
            "GSI-Obsolete": {"partition_key": "obsolete", "sort_key": None},  # remote-only
        },
    )
    dh.put_items(name, [{"pk": "a", "gsi_a": "x", "gsi_b": "y", "wrong_a": "w", "obsolete": "o"}])

    apply = run_schema_sync(tables="reconcile_all_gsi_table", apply=True, extra_env=PRUNE_ENV)
    assert apply.returncode == 0, output(apply)

    live = dh.describe_schema(name)["gsis"]
    assert set(live) == {"GSI-A", "GSI-B"}
    assert live["GSI-A"]["partition_key"] == "gsi_a"
    assert live["GSI-B"]["partition_key"] == "gsi_b"

    assert_no_table_recreate(apply)
    assert {i["pk"] for i in dh.scan_items(name)} == {"a"}
    assert dh.list_dump_objects(dump_bucket, name) == []

    second = run_schema_sync(tables="reconcile_all_gsi_table", extra_env=PRUNE_ENV)
    assert second.returncode == 0, output(second)


@pytest.mark.parametrize("prune", ["false", "true"])
def test_dry_run_reports_changed_gsi_regardless_of_prune_flag(managed_table, prune):
    # The diff report describes schema facts; only the *repair* depends on the
    # flag. A dry run must signal drift either way.
    name = managed_table(sc.changed_gsi_pk_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI1-Test": {"partition_key": "old_gsi_pk", "sort_key": None}},
    )

    dry = run_schema_sync(
        tables="changed_gsi_pk_table", extra_env={"DYNAMO_PRUNE_UNDECLARED": prune}
    )
    assert dry.returncode == 3, output(dry)
    assert "GSI1-Test" in dry.stdout, output(dry)
    assert "rebuild_gsi" in dry.stdout, output(dry)
    assert dh.describe_schema(name)["gsis"]["GSI1-Test"]["partition_key"] == "old_gsi_pk"


# ──────────────────────────────── projection ────────────────────────────────
# Projection cannot be altered in place, so getting it wrong is permanent. The
# rule: a declared projection is authoritative; an undeclared one is inherited,
# never reset to the ALL default.


def test_declared_keys_only_projection_is_created(managed_table):
    name = managed_table(sc.declared_keys_only_gsi_table)

    apply = run_schema_sync(tables="declared_keys_only_gsi_table", apply=True)

    assert apply.returncode == 0, output(apply)
    assert dh.describe_projections(name) == {"GSI1-Test": ("KEYS_ONLY", None)}
    assert run_schema_sync(tables="declared_keys_only_gsi_table").returncode == 0


def test_declared_include_projection_is_created(managed_table):
    name = managed_table(sc.declared_include_gsi_table)

    apply = run_schema_sync(tables="declared_include_gsi_table", apply=True)

    assert apply.returncode == 0, output(apply)
    assert dh.describe_projections(name) == {"GSI1-Test": ("INCLUDE", ("payload",))}
    assert run_schema_sync(tables="declared_include_gsi_table").returncode == 0


def test_declared_projection_conflict_rebuilds_the_index(managed_table, dump_bucket):
    name = managed_table(sc.declared_keys_only_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI1-Test": {"partition_key": "gsi_pk", "sort_key": None, "projection": "ALL"}},
    )
    dh.put_items(name, [{"pk": "a", "gsi_pk": "g"}])

    apply = run_schema_sync(tables="declared_keys_only_gsi_table", apply=True)

    assert apply.returncode == 0, output(apply)
    assert dh.describe_projections(name) == {"GSI1-Test": ("KEYS_ONLY", None)}
    assert_no_table_recreate(apply)
    assert {i["pk"] for i in dh.scan_items(name)} == {"a"}


def test_rebuild_inherits_unmodeled_projection_instead_of_widening_to_all(
    managed_table, dump_bucket
):
    # The KEYS_ONLY -> ALL hazard. The index key conflicts so it must be rebuilt,
    # but its projection is unmodeled: rebuilding it as ALL would silently and
    # permanently multiply the index's storage.
    name = managed_table(sc.inherit_projection_on_rebuild_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={
            "GSI1-Test": {
                "partition_key": "old_gsi_pk",
                "sort_key": None,
                "projection": "KEYS_ONLY",
            }
        },
    )
    dh.put_items(name, [{"pk": "a", "gsi_pk": "g", "old_gsi_pk": "o"}])

    apply = run_schema_sync(tables="inherit_projection_on_rebuild_table", apply=True)

    assert apply.returncode == 0, output(apply)
    assert dh.describe_schema(name)["gsis"]["GSI1-Test"]["partition_key"] == "gsi_pk"
    assert dh.describe_projections(name) == {"GSI1-Test": ("KEYS_ONLY", None)}
    assert_no_table_recreate(apply)
