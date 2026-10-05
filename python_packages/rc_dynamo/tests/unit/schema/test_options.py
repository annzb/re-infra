"""TableOptions: the UNMANAGED default, validation, and the schema-dict forms."""

from typing import Any, ClassVar

import pytest

from rc_dynamo import BaseItem, BaseTable
from rc_dynamo.schema.options import OPTION_NAMES, UNMANAGED, TableOptions, is_managed


def test_every_option_defaults_to_unmanaged():
    options = TableOptions()
    assert all(value is UNMANAGED for value in options.as_schema().values())
    assert set(options.as_schema()) == set(OPTION_NAMES)
    assert options.managed() == {}


def test_managed_lists_only_declared_options_including_explicit_off():
    options = TableOptions(point_in_time_recovery=False, ttl_attribute=None, table_class="STANDARD")
    # False and None are declarations ("off"), not "unmanaged".
    assert options.managed() == {
        "point_in_time_recovery": False,
        "ttl_attribute": None,
        "table_class": "STANDARD",
    }


def test_unmanaged_has_no_truth_value():
    # `if options.point_in_time_recovery:` must not silently pick a side.
    with pytest.raises(TypeError, match="UNMANAGED"):
        bool(UNMANAGED)
    assert repr(UNMANAGED) == "UNMANAGED"


def test_is_managed_treats_absent_as_unmanaged():
    assert is_managed({"ttl_attribute": None}, "ttl_attribute") is True
    assert is_managed({"ttl_attribute": UNMANAGED}, "ttl_attribute") is False
    assert is_managed({}, "ttl_attribute") is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"billing_mode": "ON_DEMAND"},
        {"billing_mode": None},
        {"table_class": "INFREQUENT"},
        {"stream_view_type": "NEW"},
    ],
)
def test_unknown_choices_are_rejected(kwargs: dict[str, Any]):
    with pytest.raises(ValueError, match="TableOptions"):
        TableOptions(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"point_in_time_recovery": "false"},
        {"deletion_protection": 1},
        {"ttl_attribute": ""},
        {"ttl_attribute": 3},
    ],
)
def test_wrongly_typed_values_are_rejected(kwargs: dict[str, Any]):
    with pytest.raises(TypeError, match="TableOptions"):
        TableOptions(**kwargs)


def test_options_are_frozen():
    options = TableOptions(ttl_attribute="expiresAt")
    with pytest.raises(AttributeError):
        options.ttl_attribute = "other"  # type: ignore[misc]


class _Item(BaseItem):
    partition_key: ClassVar[str] = "pk"
    pk: str


def test_table_options_must_be_a_table_options_instance():
    with pytest.raises(TypeError, match="table_options"):

        class _Bad(BaseTable[_Item]):
            table_name = "bad"
            item_model = _Item
            table_options = ("ttl_attribute", "x")  # type: ignore[assignment]


def test_tables_without_options_are_entirely_unmanaged():
    class _Plain(BaseTable[_Item]):
        table_name = "plain"
        item_model = _Item

    assert _Plain.table_options == TableOptions()
