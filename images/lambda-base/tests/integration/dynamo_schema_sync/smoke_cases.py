"""Minimal schema module for the ``--tables all`` selection test.

Kept separate from ``schema_cases`` so ``--tables all`` only ever creates a
single table. Also declares a BaseTable instance that is NOT registered in
``TABLES`` and a non-table constant, to prove selection reads only the registry.
"""
from __future__ import annotations

from typing import ClassVar, Optional

from rc_lambda_base.dynamo.base_item import BaseItem
from rc_lambda_base.dynamo.base_table import BaseTable

from .schema_cases import table_name


class SmokeItem(BaseItem):
    partition_key: ClassVar[str] = "pk"
    pk: str
    payload: Optional[str] = None


class AllSmokeTable(BaseTable[SmokeItem]):
    table_name = table_name("all_smoke")
    item_model = SmokeItem


class UnregisteredSmokeTable(BaseTable[SmokeItem]):
    table_name = table_name("unregistered_smoke")
    item_model = SmokeItem


all_smoke_table = AllSmokeTable()

# A declared table missing from TABLES: must never be created.
unregistered_smoke_table = UnregisteredSmokeTable()

# Non-table constant: must be ignored by table selection.
NOT_A_TABLE = "sentinel-not-a-table"

# Selected by `--tables all`.
TABLES = {"all_smoke_table": all_smoke_table}
