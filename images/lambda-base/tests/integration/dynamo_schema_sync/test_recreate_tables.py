"""Recreate-required behaviors: guard, dump/restore, and row filtering.

Only a *primary key* or key attribute type change requires a table recreate. A
declared GSI whose live shape differs is rebuilt in place instead, ungated --
see test_gsi_updates.py.

Every recreate test asserts both:
  1. the guard blocks recreate when DYNAMO_ALLOW_TABLE_RECREATE is not set, and
  2. an allowed recreate succeeds, restores valid rows, skips unrestorable rows,
     and cleans up its S3 dump object.
"""
from __future__ import annotations

from . import dynamo_helpers as dh
from . import schema_cases as sc
from .cli_helpers import output, run_schema_sync

RECREATE_ENV = {"DYNAMO_ALLOW_TABLE_RECREATE": "true"}


def test_recreate_required_when_partition_key_changes(managed_table, dump_bucket):
    name = managed_table(sc.recreate_pk_changed_table)
    dh.create_live_table(name, {"partition_key": "old_pk", "sort_key": None})
    dh.put_items(
        name,
        [
            {"old_pk": "1", "pk": "keep-1", "payload": "a"},
            {"old_pk": "2", "pk": "keep-2", "payload": "b"},
            {"old_pk": "3", "payload": "no-new-pk"},  # missing new pk -> skipped
        ],
    )

    blocked = run_schema_sync(tables="recreate_pk_changed_table", apply=True)
    assert blocked.returncode != 0, output(blocked)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "old_pk", "sort_key": None}
    assert len(dh.scan_items(name)) == 3

    ok = run_schema_sync(tables="recreate_pk_changed_table", apply=True, extra_env=RECREATE_ENV)
    assert ok.returncode == 0, output(ok)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": None}
    assert {i["pk"] for i in dh.scan_items(name)} == {"keep-1", "keep-2"}
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_recreate_required_when_sort_key_is_added(managed_table, dump_bucket):
    name = managed_table(sc.recreate_sk_added_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": None})
    dh.put_items(
        name,
        [
            {"pk": "a", "sk": "1"},
            {"pk": "b", "sk": "2"},
            {"pk": "c"},  # missing sk -> skipped
        ],
    )

    blocked = run_schema_sync(tables="recreate_sk_added_table", apply=True)
    assert blocked.returncode != 0, output(blocked)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": None}

    ok = run_schema_sync(tables="recreate_sk_added_table", apply=True, extra_env=RECREATE_ENV)
    assert ok.returncode == 0, output(ok)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": "sk"}
    assert {(i["pk"], i["sk"]) for i in dh.scan_items(name)} == {("a", "1"), ("b", "2")}
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_recreate_required_when_sort_key_is_removed(managed_table, dump_bucket):
    name = managed_table(sc.recreate_sk_removed_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": "old_sk"})
    dh.put_items(
        name,
        [
            {"pk": "a", "old_sk": "1"},
            {"pk": "b", "old_sk": "1"},
            {"pk": "c", "old_sk": "1"},
        ],
    )

    blocked = run_schema_sync(tables="recreate_sk_removed_table", apply=True)
    assert blocked.returncode != 0, output(blocked)

    ok = run_schema_sync(tables="recreate_sk_removed_table", apply=True, extra_env=RECREATE_ENV)
    assert ok.returncode == 0, output(ok)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": None}
    assert {i["pk"] for i in dh.scan_items(name)} == {"a", "b", "c"}
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_recreate_required_when_sort_key_name_changes(managed_table, dump_bucket):
    name = managed_table(sc.recreate_sk_name_changed_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": "old_sk"})
    dh.put_items(
        name,
        [
            {"pk": "a", "old_sk": "x", "sk": "1"},
            {"pk": "b", "old_sk": "y"},  # missing new sk -> skipped
        ],
    )

    blocked = run_schema_sync(tables="recreate_sk_name_changed_table", apply=True)
    assert blocked.returncode != 0, output(blocked)

    ok = run_schema_sync(
        tables="recreate_sk_name_changed_table", apply=True, extra_env=RECREATE_ENV
    )
    assert ok.returncode == 0, output(ok)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": "sk"}
    assert {(i["pk"], i["sk"]) for i in dh.scan_items(name)} == {("a", "1")}
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_strict_restore_filters_unknown_columns_and_validates_model(managed_table, dump_bucket):
    name = managed_table(sc.strict_restore_table)
    # Live pk is old_pk; the declaration renames it to pk. Rows carry both, so
    # nothing is skipped for a missing key and restore filtering is what shows.
    dh.create_live_table(name, {"partition_key": "old_pk", "sort_key": None})
    dh.put_items(
        name,
        [
            {"old_pk": "a", "pk": "valid", "count": 5, "unknown_col": "drop-me"},
            {"old_pk": "b", "pk": "invalid", "count": "not-a-number"},  # invalid -> skipped
        ],
    )

    ok = run_schema_sync(tables="strict_restore_table", apply=True, extra_env=RECREATE_ENV)
    assert ok.returncode == 0, output(ok)

    items = {i["pk"]: i for i in dh.scan_items(name)}
    assert set(items) == {"valid"}
    assert "unknown_col" not in items["valid"]  # unknown column filtered out
    assert "old_pk" not in items["valid"]  # not a model field -> filtered out
    assert int(items["valid"]["count"]) == 5
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_permissive_restore_preserves_unknown_columns(managed_table, dump_bucket):
    name = managed_table(sc.permissive_restore_table)
    dh.create_live_table(name, {"partition_key": "old_pk", "sort_key": None})
    dh.put_items(
        name,
        [{"old_pk": "x", "pk": "a", "ratio": 0.25, "extra_note": "keep-me"}],
    )

    ok = run_schema_sync(tables="permissive_restore_table", apply=True, extra_env=RECREATE_ENV)
    assert ok.returncode == 0, output(ok)

    items = {i["pk"]: i for i in dh.scan_items(name)}
    assert set(items) == {"a"}
    assert items["a"]["extra_note"] == "keep-me"  # unknown column preserved
    assert float(items["a"]["ratio"]) == 0.25  # numeric round-trips through the dump
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_recreate_restores_more_than_one_batch(managed_table, dump_bucket):
    name = managed_table(sc.batch_restore_table)
    dh.create_live_table(name, {"partition_key": "old_pk", "sort_key": None})
    dh.put_items(name, [{"old_pk": f"old-{i}", "pk": f"row-{i}"} for i in range(30)])

    ok = run_schema_sync(tables="batch_restore_table", apply=True, extra_env=RECREATE_ENV)
    assert ok.returncode == 0, output(ok)
    assert len(dh.scan_items(name)) == 30
    assert "Restored 30 rows" in ok.stdout, output(ok)
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_recreate_inherits_unmodeled_gsi_projection(managed_table, dump_bucket):
    # A recreate re-derives GSIs from the declaration, so the live projection has
    # to be captured before the drop. Otherwise a KEYS_ONLY index silently comes
    # back as ALL -- permanently, since projection cannot be altered in place.
    name = managed_table(sc.inherit_projection_on_recreate_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={
            "GSI1-Test": {"partition_key": "gsi_pk", "sort_key": None, "projection": "KEYS_ONLY"}
        },
    )
    dh.put_items(name, [{"pk": "a", "sk": "1", "gsi_pk": "g"}])

    ok = run_schema_sync(
        tables="inherit_projection_on_recreate_table", apply=True, extra_env=RECREATE_ENV
    )

    assert ok.returncode == 0, output(ok)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": "sk"}
    assert dh.describe_projections(name) == {"GSI1-Test": ("KEYS_ONLY", None)}
    assert {(i["pk"], i["sk"]) for i in dh.scan_items(name)} == {("a", "1")}
    assert dh.list_dump_objects(dump_bucket, name) == []


def test_recreate_preserves_undeclared_gsis(managed_table, dump_bucket):
    # Recreating a table must not become a back door for deleting indexes the
    # code does not declare -- that is DYNAMO_PRUNE_UNDECLARED's job, and it is off.
    name = managed_table(sc.preserve_undeclared_on_recreate_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"Console-Made": {"partition_key": "console_attr", "sort_key": None}},
    )
    dh.put_items(name, [{"pk": "a", "sk": "1", "console_attr": "c"}])

    ok = run_schema_sync(
        tables="preserve_undeclared_on_recreate_table", apply=True, extra_env=RECREATE_ENV
    )

    assert ok.returncode == 0, output(ok)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": "sk"}
    assert "Console-Made" in dh.describe_schema(name)["gsis"]
    assert dh.list_dump_objects(dump_bucket, name) == []
