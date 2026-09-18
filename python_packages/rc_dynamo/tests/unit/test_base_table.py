"""Tests for BaseTable create/update/get/delete/query_filter + Decimal↔float."""

from collections.abc import Mapping
from decimal import Decimal
from typing import ClassVar

import pytest
from boto3.dynamodb.conditions import Key

from rc_dynamo.base_item import BaseItem
from rc_dynamo.base_table import (
    BaseTable,
    ItemAlreadyExistsError,
    ItemDoesNotExistError,
)

from .fakes import FakeDynamoTable


class _Widget(BaseItem):
    partition_key: ClassVar[str] = "pk"
    sort_key: ClassVar[str] = "sk"
    gsis: ClassVar[Mapping[str, str]] = {"category": "GSI1-Category"}

    pk: str
    sk: str
    category: str | None = None
    weight: float | None = None
    note: str | None = None


class _WidgetTable(BaseTable[_Widget]):
    table_name = "WidgetTable"
    item_model = _Widget


def _table():
    return _WidgetTable(FakeDynamoTable(key_fields=("pk", "sk")))


# ───────────────────────── create ─────────────────────────


def test_create_serializes_floats_to_decimal_and_drops_none():
    t = _table()
    item = t.create(pk="a", sk="1", weight=1.5)
    stored = t.table.put_calls[0]
    assert stored["weight"] == Decimal("1.5")
    assert "note" not in stored  # None dropped
    assert isinstance(item, _Widget)


def test_create_generates_partition_key_when_missing():
    t = _table()
    item = t.create(sk="1")
    assert item.pk and len(item.pk) == 32  # uuid4().hex


def test_create_raises_when_item_already_exists():
    t = _table()
    t.create(pk="a", sk="1")
    with pytest.raises(ItemAlreadyExistsError):
        t.create(pk="a", sk="1")


# ───────────────────────── get_item ─────────────────────────


def test_get_item_round_trips_decimal_back_to_float():
    t = _table()
    t.create(pk="a", sk="1", weight=2.25, note="hi")
    model = t.get_item(("a", "1"))
    assert isinstance(model, _Widget)
    assert model.weight == 2.25 and isinstance(model.weight, float)
    assert model.note == "hi"


def test_get_item_missing_returns_none():
    assert _table().get_item(("x", "y")) is None


def test_get_item_projection_keeps_key_fields_for_model_validation():
    t = _table()
    t.create(pk="a", sk="1", note="visible", category="hidden")

    model = t.get_item(("a", "1"), ProjectionExpression="note")

    assert isinstance(model, _Widget)
    assert model.pk == "a"
    assert model.sk == "1"
    assert model.note == "visible"
    assert model.category is None


def test_query_projection_keeps_key_fields_for_model_validation():
    t = _table()
    t.create(pk="a", sk="1", note="visible")

    page = t.query_page(KeyConditionExpression=Key("pk").eq("a"), ProjectionExpression="note")

    assert len(page["Items"]) == 1
    model = page["Items"][0]
    assert isinstance(model, _Widget)
    assert model.pk == "a"
    assert model.sk == "1"
    assert model.note == "visible"


# ───────────────────────── update / delete ─────────────────────────


def test_update_raises_when_item_missing():
    t = _table()
    with pytest.raises(ItemDoesNotExistError):
        t.update(_Widget(pk="a", sk="1"))


def test_update_overwrites_existing():
    t = _table()
    t.create(pk="a", sk="1", note="old")
    t.update(_Widget(pk="a", sk="1", note="new"))
    assert t.get_item(("a", "1")).note == "new"


def test_update_mapping_generates_set_expression_and_preserves_omitted_fields():
    t = _table()
    t.create(pk="a", sk="1", note="old", category="keep")
    updated = t.update({"pk": "a", "sk": "1", "note": "new"})
    assert updated.note == "new"
    assert updated.category == "keep"
    call = t.table.update_calls[-1]
    assert call["Key"] == {"pk": "a", "sk": "1"}
    assert call["UpdateExpression"] == "SET #u0 = :u0"
    assert call["ExpressionAttributeNames"]["#u0"] == "note"
    assert call["ExpressionAttributeValues"][":u0"] == "new"


def test_update_mapping_writes_none_as_null_value():
    t = _table()
    t.create(pk="a", sk="1", note="old")
    updated = t.update({"pk": "a", "sk": "1", "note": None})
    assert updated.note is None
    assert "note" in t.table.get_item(Key={"pk": "a", "sk": "1"})["Item"]


def test_update_mapping_requires_all_key_fields():
    t = _table()
    with pytest.raises(ValueError, match="sk"):
        t.update({"pk": "a", "note": "new"})


def test_delete_removes_item():
    t = _table()
    t.create(pk="a", sk="1")
    t.delete(("a", "1"))
    assert t.get_item(("a", "1")) is None


# ───────────────────────── query_filter ─────────────────────────


def test_query_filter_uses_base_key():
    t = _table()
    t.create(pk="a", sk="1")
    t.create(pk="a", sk="2")
    t.create(pk="b", sk="1")
    models = t.query_filter(pk="a")
    assert {m.sk for m in models} == {"1", "2"}
    assert all(isinstance(m, _Widget) for m in models)


def test_query_filter_selects_gsi():
    t = _table()
    t.create(pk="a", sk="1", category="x")
    t.create(pk="b", sk="1", category="x")
    t.create(pk="c", sk="1", category="y")
    assert {m.pk for m in t.query_filter(category="x")} == {"a", "b"}


def test_query_filter_falls_back_to_scan_for_non_key_field():
    t = _table()
    t.create(pk="a", sk="1", note="hi")
    t.create(pk="b", sk="1", note="bye")
    assert [m.pk for m in t.query_filter(note="hi")] == ["a"]


# ───────────────────────── expected_schema / gsi_schemas ─────────────────────────


class _Gadget(BaseItem):
    partition_key: ClassVar[str] = "pk"
    gsis: ClassVar[Mapping[str, str]] = {"owner": "owner-index"}
    gsi_schemas: ClassVar[Mapping[str, Mapping[str, str | None]]] = {
        "owner-viewer-index": {"partition_key": "owner", "sort_key": "viewer"},
    }

    pk: str
    owner: str | None = None
    viewer: str | None = None


class _GadgetTable(BaseTable[_Gadget]):
    table_name = "GadgetTable"
    item_model = _Gadget


def test_expected_schema_includes_composite_gsi_schemas():
    schema = _GadgetTable(FakeDynamoTable(key_fields=("pk", None))).expected_schema()
    # The shorthand cannot express a sort key or a projection, so both are None
    # = "not modeled" (schema_diff leaves the live values alone).
    assert schema["gsis"]["owner-index"] == {
        "partition_key": "owner",
        "sort_key": None,
        "projection": None,
        "non_key_attributes": None,
    }
    assert schema["gsis"]["owner-viewer-index"] == {
        "partition_key": "owner",
        "sort_key": "viewer",
        "projection": None,
        "non_key_attributes": None,
    }
    # sort-key attributes participate in attribute definitions
    assert schema["attribute_types"]["viewer"] == "S"


def test_expected_schema_carries_declared_projection():
    class _Projected(BaseItem):
        partition_key: ClassVar[str] = "pk"
        gsi_schemas: ClassVar[Mapping[str, Mapping[str, object]]] = {
            "keys-only-index": {
                "partition_key": "owner",
                "projection": "KEYS_ONLY",
            },
            "include-index": {
                "partition_key": "viewer",
                "projection": "INCLUDE",
                "non_key_attributes": ["owner", "pk"],
            },
        }

        pk: str
        owner: str | None = None
        viewer: str | None = None

    class _ProjectedTable(BaseTable[_Projected]):
        table_name = "ProjectedTable"
        item_model = _Projected

    schema = _ProjectedTable(FakeDynamoTable(key_fields=("pk", None))).expected_schema()

    assert schema["gsis"]["keys-only-index"]["projection"] == "KEYS_ONLY"
    assert schema["gsis"]["keys-only-index"]["non_key_attributes"] is None
    assert schema["gsis"]["include-index"]["projection"] == "INCLUDE"
    # Normalized to a set: describe_table returns NonKeyAttributes unordered.
    assert schema["gsis"]["include-index"]["non_key_attributes"] == frozenset({"owner", "pk"})


def test_gsi_schemas_overlay_wins_over_gsis_shorthand():
    class _Overlay(BaseItem):
        partition_key: ClassVar[str] = "pk"
        gsis: ClassVar[Mapping[str, str]] = {"owner": "shared-index"}
        gsi_schemas: ClassVar[Mapping[str, Mapping[str, str | None]]] = {
            "shared-index": {"partition_key": "owner", "sort_key": "viewer"},
        }

        pk: str
        owner: str | None = None
        viewer: str | None = None

    class _OverlayTable(BaseTable[_Overlay]):
        table_name = "OverlayTable"
        item_model = _Overlay

    schema = _OverlayTable(FakeDynamoTable(key_fields=("pk", None))).expected_schema()
    assert schema["gsis"]["shared-index"] == {
        "partition_key": "owner",
        "sort_key": "viewer",
        "projection": None,
        "non_key_attributes": None,
    }


def test_gsi_schemas_rejects_unknown_fields():
    with pytest.raises(TypeError, match="gsi_schemas"):

        class _Bad(BaseItem):
            partition_key: ClassVar[str] = "pk"
            gsi_schemas: ClassVar[Mapping[str, Mapping[str, str | None]]] = {
                "bad-index": {"partition_key": "nope"},
            }

            pk: str


def test_gsi_schemas_requires_partition_key():
    with pytest.raises(TypeError, match="partition_key"):

        class _NoPk(BaseItem):
            partition_key: ClassVar[str] = "pk"
            gsi_schemas: ClassVar[Mapping[str, Mapping[str, str | None]]] = {
                "bad-index": {"sort_key": "pk"},
            }

            pk: str


def test_gsi_schemas_rejects_unknown_keys():
    # A misspelled key used to be accepted and then silently dropped by
    # expected_schema(), so the declaration looked like it had taken effect.
    with pytest.raises(TypeError, match="unknown key"):

        class _Typo(BaseItem):
            partition_key: ClassVar[str] = "pk"
            gsi_schemas: ClassVar[Mapping[str, Mapping[str, object]]] = {
                "idx": {"partition_key": "pk", "projection_type": "KEYS_ONLY"},
            }

            pk: str


def test_gsi_schemas_rejects_unknown_projection():
    with pytest.raises(TypeError, match="projection"):

        class _BadProjection(BaseItem):
            partition_key: ClassVar[str] = "pk"
            gsi_schemas: ClassVar[Mapping[str, Mapping[str, object]]] = {
                "idx": {"partition_key": "pk", "projection": "keys_only"},
            }

            pk: str


def test_gsi_schemas_requires_non_key_attributes_for_include():
    with pytest.raises(TypeError, match="non_key_attributes"):

        class _EmptyInclude(BaseItem):
            partition_key: ClassVar[str] = "pk"
            gsi_schemas: ClassVar[Mapping[str, Mapping[str, object]]] = {
                "idx": {"partition_key": "pk", "projection": "INCLUDE"},
            }

            pk: str


def test_gsi_schemas_rejects_non_key_attributes_without_include():
    with pytest.raises(TypeError, match="not INCLUDE"):

        class _StrayAttrs(BaseItem):
            partition_key: ClassVar[str] = "pk"
            gsi_schemas: ClassVar[Mapping[str, Mapping[str, object]]] = {
                "idx": {"partition_key": "pk", "projection": "ALL", "non_key_attributes": ["x"]},
            }

            pk: str


def test_gsi_schemas_allows_include_attributes_that_are_not_model_fields():
    # Permissive models carry real attributes that are not declared fields;
    # requiring them here would make adopting a live INCLUDE index impossible.
    class _Include(BaseItem):
        partition_key: ClassVar[str] = "pk"
        gsi_schemas: ClassVar[Mapping[str, Mapping[str, object]]] = {
            "idx": {
                "partition_key": "pk",
                "projection": "INCLUDE",
                "non_key_attributes": ["NotAField"],
            },
        }

        pk: str

    assert _Include.gsi_schemas["idx"]["non_key_attributes"] == ["NotAField"]
