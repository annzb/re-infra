"""Schema tooling: the diff vocabulary, the apply engine, and the drift report.

Only :mod:`rc_dynamo.schema.diff` and :mod:`rc_dynamo.schema.options` are
re-exported here. Both are leaf modules, so importing them from this package is
safe; ``sync`` and ``report`` import
:class:`~rc_dynamo.base_table.BaseTable`, which imports this package in turn, so
they must be imported from their own modules:

    from rc_dynamo.schema.sync import sync_tables
    from rc_dynamo.schema.report import build_report
"""

from rc_dynamo.schema.diff import (
    Finding,
    FindingKind,
    Permission,
    Remedy,
    SchemaDiff,
    Severity,
    diff_schemas,
    resolve_projection,
)
from rc_dynamo.schema.options import UNMANAGED, TableOptions, Unmanaged

__all__ = [
    "UNMANAGED",
    "Finding",
    "FindingKind",
    "Permission",
    "Remedy",
    "SchemaDiff",
    "Severity",
    "TableOptions",
    "Unmanaged",
    "diff_schemas",
    "resolve_projection",
]
