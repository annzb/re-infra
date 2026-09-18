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

__version__ = "0.1.0"

__all__ = [
    "BaseItem",
    "BaseTable",
    "ItemAlreadyExistsError",
    "ItemDoesNotExistError",
    "ItemType",
    "KeyType",
    "QueryPlan",
    "TableError",
]
