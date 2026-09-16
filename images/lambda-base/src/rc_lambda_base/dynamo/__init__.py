"""Generic DynamoDB framework: declarative items and tables plus schema tooling.

Application table declarations live in the application. Its schema module
exports them as ``TABLES: Mapping[str, BaseTable]`` for ``rc-dynamo-sync`` and
``rc-dynamo-report``.
"""
from rc_lambda_base.dynamo.base_item import BaseItem, ItemType, KeyType
from rc_lambda_base.dynamo.base_table import (
    BaseTable,
    ItemAlreadyExistsError,
    ItemDoesNotExistError,
    QueryPlan,
    TableError,
)

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
