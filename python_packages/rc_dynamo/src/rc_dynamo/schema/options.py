"""Declarative table-level settings: billing, table class, protections, TTL, streams.

Keys and indexes describe the *shape* of a table; ``TableOptions`` describes how it
is operated. It is declared on the table, not the item, because one item model can
back tables that are operated differently::

    class SessionsTable(BaseTable[Session]):
        table_name = "rc-sessions"
        item_model = Session
        table_options = TableOptions(
            ttl_attribute="expiresAt",
            point_in_time_recovery=True,
            deletion_protection=True,
        )

The governing rule
------------------
**Every field defaults to ``UNMANAGED``, and ``UNMANAGED`` never means "off".**
It means "not modeled -- do not enforce, inherit what is live", the same rule a
GSI's ``sort_key=None`` / ``projection=None`` follows. Turning something off is
always explicit: ``point_in_time_recovery=False``, ``ttl_attribute=None``,
``stream_view_type=None``.

``None`` is a real value for ``ttl_attribute`` and ``stream_view_type`` (TTL /
streams disabled), which is exactly why "not modeled" needs its own sentinel
instead of reusing ``None`` as the GSI fields do.

An unmanaged setting is still reproduced where that is possible: a table recreate
carries the live TTL, PITR and deletion-protection settings over to the new table.
Settings this tooling cannot reproduce at all (LSIs, KMS encryption, replicas, an
undeclared stream, ...) are reported by the diff and make a recreate refuse.

Leaf module: no boto, no pydantic, no import of ``base_table``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Final, Literal, TypeAlias


class Unmanaged(Enum):
    """Type of the ``UNMANAGED`` sentinel. Single-member, so mypy narrows on ``is``."""

    UNMANAGED = "unmanaged"

    def __repr__(self) -> str:
        return "UNMANAGED"

    def __bool__(self) -> bool:
        # `if options.point_in_time_recovery:` would silently treat UNMANAGED as
        # either on or off. Force callers to compare with `is UNMANAGED`.
        raise TypeError("UNMANAGED has no truth value; compare with `is UNMANAGED`")


UNMANAGED: Final = Unmanaged.UNMANAGED

BILLING_PAY_PER_REQUEST = "PAY_PER_REQUEST"
BILLING_PROVISIONED = "PROVISIONED"
BILLING_MODES = (BILLING_PAY_PER_REQUEST, BILLING_PROVISIONED)

TABLE_CLASS_STANDARD = "STANDARD"
TABLE_CLASS_INFREQUENT_ACCESS = "STANDARD_INFREQUENT_ACCESS"
TABLE_CLASSES = (TABLE_CLASS_STANDARD, TABLE_CLASS_INFREQUENT_ACCESS)

STREAM_VIEW_TYPES = ("KEYS_ONLY", "NEW_IMAGE", "OLD_IMAGE", "NEW_AND_OLD_IMAGES")

BillingMode: TypeAlias = Literal["PAY_PER_REQUEST", "PROVISIONED"]
TableClass: TypeAlias = Literal["STANDARD", "STANDARD_INFREQUENT_ACCESS"]
StreamViewType: TypeAlias = Literal["KEYS_ONLY", "NEW_IMAGE", "OLD_IMAGE", "NEW_AND_OLD_IMAGES"]

# Option names, in the order findings and logs list them.
OPTION_NAMES = (
    "billing_mode",
    "table_class",
    "deletion_protection",
    "point_in_time_recovery",
    "ttl_attribute",
    "stream_view_type",
)


@dataclass(frozen=True)
class TableOptions:
    """Operational settings of a table. See the module docstring for ``UNMANAGED``.

    billing_mode
        ``PAY_PER_REQUEST`` or ``PROVISIONED``. ``PROVISIONED`` can only be
        *acknowledged*: capacity and autoscaling are not modeled, so the sync never
        creates a provisioned table or switches one to provisioned. It will switch
        a provisioned table to on-demand when ``PAY_PER_REQUEST`` is declared.
    table_class
        ``STANDARD`` or ``STANDARD_INFREQUENT_ACCESS``.
    deletion_protection
        Enabling is ungated; disabling needs DYNAMO_ALLOW_PROTECTION_DOWNGRADE.
        A table with deletion protection enabled live is never recreated.
    point_in_time_recovery
        Enabling is ungated; disabling discards the restore window, so it needs
        DYNAMO_ALLOW_PROTECTION_DOWNGRADE.
    ttl_attribute
        Attribute name to enable TTL on, or ``None`` for TTL disabled.
    stream_view_type
        ``KEYS_ONLY`` / ``NEW_IMAGE`` / ``OLD_IMAGE`` / ``NEW_AND_OLD_IMAGES``, or
        ``None`` for streams disabled.
    """

    billing_mode: BillingMode | Unmanaged = UNMANAGED
    table_class: TableClass | Unmanaged = UNMANAGED
    deletion_protection: bool | Unmanaged = UNMANAGED
    point_in_time_recovery: bool | Unmanaged = UNMANAGED
    ttl_attribute: str | Unmanaged | None = UNMANAGED
    stream_view_type: StreamViewType | Unmanaged | None = UNMANAGED

    def __post_init__(self) -> None:
        _check_choice("billing_mode", self.billing_mode, BILLING_MODES, nullable=False)
        _check_choice("table_class", self.table_class, TABLE_CLASSES, nullable=False)
        _check_choice("stream_view_type", self.stream_view_type, STREAM_VIEW_TYPES, nullable=True)
        for name in ("deletion_protection", "point_in_time_recovery"):
            value = getattr(self, name)
            # A real bool only: "false" (a str) would otherwise read as enabled.
            if value is not UNMANAGED and not isinstance(value, bool):
                raise TypeError(f"TableOptions.{name} must be a bool or UNMANAGED, not {value!r}")
        ttl = self.ttl_attribute
        if ttl is not UNMANAGED and ttl is not None and (not isinstance(ttl, str) or not ttl):
            raise TypeError(
                f"TableOptions.ttl_attribute must be a non-empty attribute name, None "
                f"(TTL disabled) or UNMANAGED, not {ttl!r}"
            )

    def as_schema(self) -> dict[str, Any]:
        """Every option, ``UNMANAGED`` included -- the form ``expected_schema()`` uses."""
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def managed(self) -> dict[str, Any]:
        """Only the options this declaration enforces."""
        return {name: value for name, value in self.as_schema().items() if value is not UNMANAGED}


def _check_choice(name: str, value: Any, choices: tuple[str, ...], *, nullable: bool) -> None:
    if value is UNMANAGED or (nullable and value is None) or value in choices:
        return
    allowed = ", ".join(choices) + (", None" if nullable else "")
    raise ValueError(f"TableOptions.{name}={value!r} is not one of {allowed} or UNMANAGED")


def is_managed(options: Mapping[str, Any], name: str) -> bool:
    """Whether a schema-dict ``options`` mapping declares ``name``. Absent = unmanaged."""
    return options.get(name, UNMANAGED) is not UNMANAGED
