"""Minimal schema module for the ``--tables all`` selection test.

Kept separate from ``schema_cases`` so ``--tables all`` only ever creates a
single table (running the full case list under ``all`` would make the Compose
setup slow). Includes a non-table constant to prove selection ignores non
-BaseTable module attributes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from rc_lambda_base.dynamo.base_item import BaseItem
from rc_lambda_base.dynamo.base_table import BaseTable

from .schema_cases import table_name


class SmokeItem(BaseItem):
    partition_key: ClassVar[str] = "pk"
    pk: str
    payload: str | None = None


class AllSmokeTable(BaseTable[SmokeItem]):
    table_name = table_name("all_smoke")
    item_model = SmokeItem


# Selected by `--tables all`.
all_smoke_table = AllSmokeTable()

# Non-table constant: must be ignored by table selection.
NOT_A_TABLE = "sentinel-not-a-table"


# The registry rc-dynamo-sync loads: selection name -> table. Keys match the
# module-level names above, so --tables <name> selects one case.
TABLES: Mapping[str, BaseTable[Any]] = {
    name: value for name, value in sorted(globals().items()) if isinstance(value, BaseTable)
}
