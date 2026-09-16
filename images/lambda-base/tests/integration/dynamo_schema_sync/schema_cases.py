"""Centralized BaseTable/BaseItem test schemas for the schema-sync tests.

All table names are namespaced by ``DYNAMO_SCHEMA_TEST_PREFIX`` + a per-run id so
concurrent/interrupted runs never collide and cleanup can target one prefix.

The migration script imports this module via ``--schema-module`` /
``DYNAMO_SCHEMA_MODULE`` and selects tables by their module-level attribute name
(e.g. ``--tables create_simple_table``). Tests set up a *live* table with a
possibly-different schema, then run the script and assert the reconciliation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import ConfigDict

from rc_lambda_base.dynamo.base_item import BaseItem
from rc_lambda_base.dynamo.base_table import BaseTable

PREFIX = os.environ["DYNAMO_SCHEMA_TEST_PREFIX"]
RUN_ID = os.environ.get("DYNAMO_SCHEMA_TEST_RUN_ID", "manual")


def table_name(case: str) -> str:
    return f"{PREFIX}-{RUN_ID}-{case}"


# ─────────────────────────── item models ───────────────────────────


class SimpleItem(BaseItem):
    partition_key: ClassVar[str] = "pk"
    pk: str
    payload: str | None = None
    count: int | None = None


class CompositeItem(BaseItem):
    partition_key: ClassVar[str] = "pk"
    sort_key: ClassVar[str] = "sk"
    pk: str
    sk: str
    payload: str | None = None


class MultiGsiItem(BaseItem):
    partition_key: ClassVar[str] = "pk"
    gsis: ClassVar[Mapping[str, str]] = {"gsi_a": "GSI-A", "gsi_b": "GSI-B"}
    pk: str
    gsi_a: str | None = None
    gsi_b: str | None = None
    payload: str | None = None


class OneGsiItem(BaseItem):
    partition_key: ClassVar[str] = "pk"
    gsis: ClassVar[Mapping[str, str]] = {"gsi_a": "GSI-A"}
    pk: str
    gsi_a: str | None = None
    payload: str | None = None


class Gsi1Item(BaseItem):
    """Declares GSI1-Test on ``gsi_pk``; ``gsi_sk`` exists as a plain attribute
    (the production model cannot express a GSI sort key)."""

    partition_key: ClassVar[str] = "pk"
    gsis: ClassVar[Mapping[str, str]] = {"gsi_pk": "GSI1-Test"}
    pk: str
    gsi_pk: str | None = None
    gsi_sk: str | None = None
    payload: str | None = None


class StrictRestoreItem(BaseItem):
    """Recreate is triggered by a primary-key *rename* (live ``old_pk`` ->
    declared ``pk``).

    A GSI key change no longer works as a trigger: those are rebuilt in place
    without touching the table. The rename keeps every seeded row restorable --
    tests seed both attributes -- so restore semantics can be tested without
    key-based row skips.
    """

    partition_key: ClassVar[str] = "pk"
    pk: str
    count: int | None = None


class PermissiveRestoreItem(BaseItem):
    model_config = ConfigDict(extra="allow")
    partition_key: ClassVar[str] = "pk"
    pk: str
    ratio: float | None = None


# ─────────────────────────── table instances ───────────────────────────
# Create cases (expected schema; test starts with no table).


class CreateSimpleTable(BaseTable[SimpleItem]):
    table_name = table_name("create_simple")
    item_model = SimpleItem


class CreateCompositeTable(BaseTable[CompositeItem]):
    table_name = table_name("create_composite")
    item_model = CompositeItem


class CreateGsiTable(BaseTable[MultiGsiItem]):
    table_name = table_name("create_gsi")
    item_model = MultiGsiItem


# GSI-only update cases (primary key matches live; GSIs differ).


class MissingGsiTable(BaseTable[OneGsiItem]):
    table_name = table_name("missing_gsi")
    item_model = OneGsiItem


class RemoteOnlyGsiTable(BaseTable[SimpleItem]):
    # Expected: pk only. Live table carries an extra, undeclared GSI.
    table_name = table_name("remote_only_gsi")
    item_model = SimpleItem


class ReconcileGsiTable(BaseTable[OneGsiItem]):
    # Expected: GSI-A. Live: an unrelated remote-only GSI (to be removed) and
    # GSI-A missing (to be created) — reconciled together when delete enabled.
    table_name = table_name("reconcile_gsi")
    item_model = OneGsiItem


class IgnoreCompositeRemoteGsiTable(BaseTable[Gsi1Item]):
    # Expected: GSI1-Test on gsi_pk (sort key unmodeled -> None). Live: GSI1-Test
    # on gsi_pk + gsi_sk. Regression: must NOT be treated as drift.
    table_name = table_name("ignore_composite_remote_gsi")
    item_model = Gsi1Item


# In-place GSI rebuild cases (drifted index; primary key matches live). A
# declared index whose live shape differs is reconciled to the declaration with
# no flag — that is what "declarations are the source of truth" means — so these
# never route to a table recreate.


class ChangedGsiPkTable(BaseTable[Gsi1Item]):
    # Expected GSI1-Test pk=gsi_pk; live GSI1-Test pk=old_gsi_pk.
    table_name = table_name("changed_gsi_pk")
    item_model = Gsi1Item


class ChangedGsiSkTable(BaseTable[Gsi1Item]):
    # Test-only override: expected GSI1-Test is composite (gsi_pk + gsi_sk).
    # Live GSI1-Test is gsi_pk HASH only.
    table_name = table_name("changed_gsi_sk")
    item_model = Gsi1Item

    def expected_schema(self) -> dict[str, Any]:
        schema = super().expected_schema()
        schema["gsis"]["GSI1-Test"] = {
            "partition_key": "gsi_pk",
            "sort_key": "gsi_sk",
            "projection": None,  # unmodeled -> inherited from live
            "non_key_attributes": None,
        }
        schema["attribute_types"].update({"gsi_pk": "S", "gsi_sk": "S"})
        return schema


class KeysOnlyProjectionItem(BaseItem):
    """Declares GSI1-Test as KEYS_ONLY, so the projection is authoritative."""

    partition_key: ClassVar[str] = "pk"
    gsi_schemas: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "GSI1-Test": {"partition_key": "gsi_pk", "projection": "KEYS_ONLY"},
    }
    pk: str
    gsi_pk: str | None = None
    payload: str | None = None


class IncludeProjectionItem(BaseItem):
    partition_key: ClassVar[str] = "pk"
    gsi_schemas: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "GSI1-Test": {
            "partition_key": "gsi_pk",
            "projection": "INCLUDE",
            "non_key_attributes": ["payload"],
        },
    }
    pk: str
    gsi_pk: str | None = None
    payload: str | None = None


class DeclaredKeysOnlyGsiTable(BaseTable[KeysOnlyProjectionItem]):
    # A declared projection is created as declared, and enforced afterwards.
    table_name = table_name("declared_keys_only_gsi")
    item_model = KeysOnlyProjectionItem


class DeclaredIncludeGsiTable(BaseTable[IncludeProjectionItem]):
    table_name = table_name("declared_include_gsi")
    item_model = IncludeProjectionItem


class InheritProjectionOnRebuildTable(BaseTable[Gsi1Item]):
    # Declares GSI1-Test via the shorthand, so the projection is unmodeled. Live
    # GSI1-Test is KEYS_ONLY on the *wrong* attribute: the key conflict forces a
    # rebuild, and the rebuild must inherit KEYS_ONLY rather than widen to ALL.
    table_name = table_name("inherit_projection_on_rebuild")
    item_model = Gsi1Item


class CompositeGsiItem(BaseItem):
    """pk+sk primary key plus a shorthand GSI (projection unmodeled)."""

    partition_key: ClassVar[str] = "pk"
    sort_key: ClassVar[str] = "sk"
    gsis: ClassVar[Mapping[str, str]] = {"gsi_pk": "GSI1-Test"}
    pk: str
    sk: str
    gsi_pk: str | None = None
    payload: str | None = None


class InheritProjectionOnRecreateTable(BaseTable[CompositeGsiItem]):
    # Expected pk+sk; live pk only -> table recreate. The declared GSI's live
    # KEYS_ONLY projection is unmodeled and must survive the drop/create.
    table_name = table_name("inherit_projection_on_recreate")
    item_model = CompositeGsiItem


class PreserveUndeclaredGsiOnRecreateTable(BaseTable[CompositeItem]):
    # Expected pk+sk; live pk only, plus an undeclared GSI. The recreate must
    # not use the table rebuild as a back door to delete undeclared work.
    table_name = table_name("preserve_undeclared_on_recreate")
    item_model = CompositeItem


class ReconcileAllGsiTable(BaseTable[MultiGsiItem]):
    # Expected: GSI-A on gsi_a, GSI-B on gsi_b. Live: GSI-A on the *wrong*
    # attribute (changed), GSI-B absent (missing), plus a remote-only index.
    # Exercises all three repair kinds in a single apply.
    table_name = table_name("reconcile_all_gsi")
    item_model = MultiGsiItem


# Recreate-required cases (primary key schema differs -> recreate).


class RecreatePkChangedTable(BaseTable[SimpleItem]):
    # Expected pk=pk; live pk=old_pk.
    table_name = table_name("recreate_pk_changed")
    item_model = SimpleItem


class RecreateSkAddedTable(BaseTable[CompositeItem]):
    # Expected pk+sk; live pk only.
    table_name = table_name("recreate_sk_added")
    item_model = CompositeItem


class RecreateSkRemovedTable(BaseTable[SimpleItem]):
    # Expected pk only; live pk+old_sk.
    table_name = table_name("recreate_sk_removed")
    item_model = SimpleItem


class RecreateSkNameChangedTable(BaseTable[CompositeItem]):
    # Expected pk+sk; live pk+old_sk.
    table_name = table_name("recreate_sk_name_changed")
    item_model = CompositeItem


# Restore-behavior cases (recreate via primary-key rename: live old_pk -> pk).


class StrictRestoreTable(BaseTable[StrictRestoreItem]):
    table_name = table_name("strict_restore")
    item_model = StrictRestoreItem
    insert_unknown_columns_on_recreate = False


class PermissiveRestoreTable(BaseTable[PermissiveRestoreItem]):
    table_name = table_name("permissive_restore")
    item_model = PermissiveRestoreItem
    insert_unknown_columns_on_recreate = True


class BatchRestoreTable(BaseTable[PermissiveRestoreItem]):
    # Same shape as permissive restore; used to seed 30+ rows and exercise the
    # 25-item batch-write boundary during restore.
    table_name = table_name("batch_restore")
    item_model = PermissiveRestoreItem
    insert_unknown_columns_on_recreate = True


# Module-level instances selected by name via --tables.
create_simple_table = CreateSimpleTable()
create_composite_table = CreateCompositeTable()
create_gsi_table = CreateGsiTable()

missing_gsi_table = MissingGsiTable()
remote_only_gsi_table = RemoteOnlyGsiTable()
reconcile_gsi_table = ReconcileGsiTable()
ignore_composite_remote_gsi_table = IgnoreCompositeRemoteGsiTable()

changed_gsi_pk_table = ChangedGsiPkTable()
changed_gsi_sk_table = ChangedGsiSkTable()
reconcile_all_gsi_table = ReconcileAllGsiTable()

declared_keys_only_gsi_table = DeclaredKeysOnlyGsiTable()
declared_include_gsi_table = DeclaredIncludeGsiTable()
inherit_projection_on_rebuild_table = InheritProjectionOnRebuildTable()
inherit_projection_on_recreate_table = InheritProjectionOnRecreateTable()
preserve_undeclared_on_recreate_table = PreserveUndeclaredGsiOnRecreateTable()

recreate_pk_changed_table = RecreatePkChangedTable()
recreate_sk_added_table = RecreateSkAddedTable()
recreate_sk_removed_table = RecreateSkRemovedTable()
recreate_sk_name_changed_table = RecreateSkNameChangedTable()

strict_restore_table = StrictRestoreTable()
permissive_restore_table = PermissiveRestoreTable()
batch_restore_table = BatchRestoreTable()


# The registry rc-dynamo-sync loads: selection name -> table. Keys match the
# module-level names above, so --tables <name> selects one case.
TABLES: Mapping[str, BaseTable[Any]] = {
    name: value for name, value in sorted(globals().items()) if isinstance(value, BaseTable)
}
