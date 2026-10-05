"""Generic DynamoDB framework: declarative items and tables plus schema tooling.

Only application-independent code lives here. Application table declarations stay
in the application; its schema module exports them as ``TABLES: Mapping[str,
BaseTable]`` for ``rc-dynamo-sync`` and ``rc-dynamo-report``.
"""

from rc_dynamo.base_item import BaseItem, ItemType, KeyType
from rc_dynamo.base_table import (
    BaseTable,
    ItemAlreadyExistsError,
    ItemDoesNotExistError,
    QueryPlan,
    TableError,
)
from rc_dynamo.schema.options import UNMANAGED, TableOptions

__version__ = "0.1.0"

__all__ = [
    "UNMANAGED",
    "BaseItem",
    "BaseTable",
    "ItemAlreadyExistsError",
    "ItemDoesNotExistError",
    "ItemType",
    "KeyType",
    "QueryPlan",
    "TableError",
    "TableOptions",
]
