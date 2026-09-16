"""Compare a declared DynamoDB table schema against the live one.

This is the single source of truth for "how does the code differ from AWS". Both
the deploy-time sync (`rc-dynamo-sync`) and the drift report
(`rc-dynamo-report`) consume it, so they can never disagree
about what changed or about what should be done in response.

Pure functions over the two dicts produced by `BaseTable.expected_schema()` and
`BaseTable.actual_schema()`. No boto, no pydantic, no import of `base_table`.

The governing rule
------------------
**`None` in a declaration means "not modeled -- do not enforce, inherit what is
live."** It applies uniformly to a GSI's `sort_key` and its `projection`.

The `BaseItem.gsis` shorthand can only express a HASH key, so every index
declared through it reports `sort_key=None` and `projection=None`. Treating that
as "the live index must have no sort key" would flag most of the live estate as
drift; treating it as "unmodeled" lets declarations be adopted incrementally.
Such gaps are still reported -- as `UNDECLARED_*` findings, whose only remedy is
a human editing the Python -- so "no conflicts" never silently means "we didn't
look".

Deliberately NOT compared
-------------------------
TTL, streams, PITR / continuous backups, tags, autoscaling, SSE, table class,
deletion protection, and **local secondary indexes**. LSIs deserve the loudest
warning: they cannot be added or removed after a table is created, so a live LSI
this model cannot see is destroyed unrecoverably by any table recreate.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

# Attribute-type codes DynamoDB allows on a key attribute.
KEY_ATTRIBUTE_TYPES = ("S", "N", "B")

PROJECTION_ALL = "ALL"
PROJECTION_KEYS_ONLY = "KEYS_ONLY"
PROJECTION_INCLUDE = "INCLUDE"
PROJECTION_TYPES = (PROJECTION_ALL, PROJECTION_KEYS_ONLY, PROJECTION_INCLUDE)


class Severity(StrEnum):
    """What kind of difference this is -- descriptive, for grouping a report."""

    CONFLICT = "conflict"      # declared and live disagree about a modeled thing
    MISSING = "missing"        # declared, absent live
    UNDECLARED = "undeclared"  # live, not modeled in Python
    INFO = "info"              # observation this tooling does not manage


class FindingKind(StrEnum):
    MISSING_TABLE = "missing_table"
    MISSING_GSI = "missing_gsi"

    UNDECLARED_GSI = "undeclared_gsi"
    UNDECLARED_SORT_KEY = "undeclared_sort_key"
    UNDECLARED_PROJECTION = "undeclared_projection"
    UNDECLARED_ATTRIBUTE = "undeclared_attribute"

    CONFLICTING_TABLE_KEY = "conflicting_table_key"
    CONFLICTING_GSI_KEY = "conflicting_gsi_key"
    CONFLICTING_PROJECTION = "conflicting_projection"
    CONFLICTING_ATTRIBUTE_TYPE = "conflicting_attribute_type"

    UNMANAGED_BILLING_MODE = "unmanaged_billing_mode"


class Remedy(StrEnum):
    """The action that resolves a finding.

    `REQUIRED_PERMISSION` maps each to the operator permission it needs. A
    remedy whose permission is not granted is what blocks a deploy.
    """

    CREATE_TABLE = "create_table"
    CREATE_GSI = "create_gsi"
    REBUILD_GSI = "rebuild_gsi"
    DELETE_GSI = "delete_gsi"
    RECREATE_TABLE = "recreate_table"
    ADOPT_DECLARATION = "adopt_declaration"
    NONE = "none"


class Permission(StrEnum):
    PRUNE_UNDECLARED = "prune_undeclared"
    ALLOW_TABLE_RECREATE = "allow_table_recreate"


# Creating what the code declares, and reconciling an index the code already
# declares, are the deploy's job and need no permission -- that is what
# "declarations are the source of truth" means. Rebuilding a GSI deletes and
# recreates one index; it cannot lose a row, because a GSI holds only derived
# data. Destroying undeclared work, or a table's rows, is opt-in.
#
# ADOPT_DECLARATION maps to None because it is never automatable: the only fix
# is a human editing the Python. Those findings are reported and skipped.
REQUIRED_PERMISSION: Mapping[Remedy, Optional[Permission]] = {
    Remedy.CREATE_TABLE: None,
    Remedy.CREATE_GSI: None,
    Remedy.REBUILD_GSI: None,
    Remedy.DELETE_GSI: Permission.PRUNE_UNDECLARED,
    Remedy.RECREATE_TABLE: Permission.ALLOW_TABLE_RECREATE,
    Remedy.ADOPT_DECLARATION: None,
    Remedy.NONE: None,
}

# Remedies the deploy executes. Everything else is report-only.
AUTOMATABLE_REMEDIES = frozenset({
    Remedy.CREATE_TABLE,
    Remedy.CREATE_GSI,
    Remedy.REBUILD_GSI,
    Remedy.DELETE_GSI,
    Remedy.RECREATE_TABLE,
})


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    severity: Severity
    remedy: Remedy
    table_name: str
    message: str
    index_name: Optional[str] = None
    attribute: Optional[str] = None
    declared: Any = None
    live: Any = None

    @property
    def required_permission(self) -> Optional[Permission]:
        return REQUIRED_PERMISSION[self.remedy]

    @property
    def is_automatable(self) -> bool:
        return self.remedy in AUTOMATABLE_REMEDIES

    def to_json(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "kind": str(self.kind),
            "severity": str(self.severity),
            "remedy": str(self.remedy),
            "table_name": self.table_name,
            "message": self.message,
        }
        if self.index_name is not None:
            payload["index_name"] = self.index_name
        if self.attribute is not None:
            payload["attribute"] = self.attribute
        if self.declared is not None:
            payload["declared"] = _jsonable(self.declared)
        if self.live is not None:
            payload["live"] = _jsonable(self.live)
        return payload


@dataclass(frozen=True)
class SchemaDiff:
    table_name: str
    findings: Tuple[Finding, ...]

    @property
    def is_clean(self) -> bool:
        return not self.findings

    def of_severity(self, *severities: Severity) -> Tuple[Finding, ...]:
        wanted = set(severities)
        return tuple(f for f in self.findings if f.severity in wanted)

    def with_remedy(self, *remedies: Remedy) -> Tuple[Finding, ...]:
        wanted = set(remedies)
        return tuple(f for f in self.findings if f.remedy in wanted)

    def actionable(self, granted: Mapping[Permission, bool]) -> Tuple[Finding, ...]:
        """Findings the deploy can and may execute, given the granted permissions."""
        return tuple(
            f for f in self.findings
            if f.is_automatable
            and (f.required_permission is None or granted.get(f.required_permission, False))
        )

    def blocked(self, granted: Mapping[Permission, bool]) -> Tuple[Finding, ...]:
        """Conflicts the deploy must resolve but is not permitted to. These fail the run.

        Only CONFLICT findings can block. An UNDECLARED finding without its
        permission is not a failure -- leaving undeclared work alone is the
        intended behaviour, so it is simply skipped.
        """
        return tuple(
            f for f in self.findings
            if f.severity is Severity.CONFLICT
            and f.is_automatable
            and f.required_permission is not None
            and not granted.get(f.required_permission, False)
        )

    def requires_action(self, granted: Mapping[Permission, bool]) -> bool:
        """Whether the live schema differs in a way the sync cares about.

        Drives the dry-run exit code. Undeclared elements are excluded: they are
        reported, but the sync will never act on them, so a deploy that leaves
        them alone has nothing to do.
        """
        return bool(self.actionable(granted) or self.blocked(granted))

    def to_json(self) -> Dict[str, Any]:
        return {
            "table_name": self.table_name,
            "findings": [f.to_json() for f in self.findings],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Mapping):
        return {key: _jsonable(inner) for key, inner in value.items()}
    return value


def normalize_non_key_attributes(value: Any) -> Optional[FrozenSet[str]]:
    """DynamoDB returns NonKeyAttributes unordered, so compare it as a set."""
    if value is None:
        return None
    return frozenset(value)


def _describe_projection(gsi: Mapping[str, Any]) -> str:
    projection = gsi.get("projection")
    if projection is None:
        return "unmodeled"
    non_key = normalize_non_key_attributes(gsi.get("non_key_attributes"))
    if projection == PROJECTION_INCLUDE and non_key:
        return f"{projection}({', '.join(sorted(non_key))})"
    return str(projection)


def resolve_projection(
    declared_gsi: Mapping[str, Any],
    live_gsi: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The projection to send to DynamoDB when creating or rebuilding an index.

    Declared wins. Otherwise inherit whatever is live -- **not** ALL. Projection
    cannot be altered in place, so defaulting an unmodeled index to ALL would
    silently and permanently widen a KEYS_ONLY index the first time anything
    rebuilt it. ALL is used only for an index that does not exist yet.
    """
    projection = declared_gsi.get("projection")
    non_key = normalize_non_key_attributes(declared_gsi.get("non_key_attributes"))

    if projection is None and live_gsi is not None:
        projection = live_gsi.get("projection")
        non_key = normalize_non_key_attributes(live_gsi.get("non_key_attributes"))

    if projection is None:
        projection = PROJECTION_ALL
        non_key = None

    resolved: Dict[str, Any] = {"ProjectionType": projection}
    if projection == PROJECTION_INCLUDE and non_key:
        resolved["NonKeyAttributes"] = sorted(non_key)
    return resolved


def _diff_gsi(
    table_name: str,
    index_name: str,
    declared: Mapping[str, Any],
    live: Mapping[str, Any],
) -> List[Finding]:
    findings: List[Finding] = []

    declared_pk = declared.get("partition_key")
    live_pk = live.get("partition_key")
    declared_sk = declared.get("sort_key")
    live_sk = live.get("sort_key")

    # A partition-key change is always a conflict; there is nothing to "adopt".
    if declared_pk != live_pk:
        findings.append(Finding(
            kind=FindingKind.CONFLICTING_GSI_KEY,
            severity=Severity.CONFLICT,
            remedy=Remedy.REBUILD_GSI,
            table_name=table_name,
            index_name=index_name,
            message=(
                f"{index_name}: declared partition key {declared_pk!r} but live is "
                f"{live_pk!r}; the index will be rebuilt to match the declaration"
            ),
            declared=dict(declared),
            live=dict(live),
        ))
    elif declared_sk is None and live_sk is not None:
        # Unmodeled sort key: the shorthand cannot express it. Report, never touch.
        findings.append(Finding(
            kind=FindingKind.UNDECLARED_SORT_KEY,
            severity=Severity.UNDECLARED,
            remedy=Remedy.ADOPT_DECLARATION,
            table_name=table_name,
            index_name=index_name,
            attribute=live_sk,
            message=(
                f"{index_name}: live index has sort key {live_sk!r}, which the "
                f"declaration does not model. Left as-is; declare it in "
                f"gsi_schemas to make it authoritative"
            ),
            declared=dict(declared),
            live=dict(live),
        ))
    elif declared_sk != live_sk:
        findings.append(Finding(
            kind=FindingKind.CONFLICTING_GSI_KEY,
            severity=Severity.CONFLICT,
            remedy=Remedy.REBUILD_GSI,
            table_name=table_name,
            index_name=index_name,
            message=(
                f"{index_name}: declared sort key {declared_sk!r} but live is "
                f"{live_sk!r}; the index will be rebuilt to match the declaration"
            ),
            declared=dict(declared),
            live=dict(live),
        ))

    declared_projection = declared.get("projection")
    live_projection = live.get("projection")
    declared_non_key = normalize_non_key_attributes(declared.get("non_key_attributes"))
    live_non_key = normalize_non_key_attributes(live.get("non_key_attributes"))

    if declared_projection is None:
        # Only worth reporting when the live projection is something a future
        # rebuild could lose. ALL is the create-time default, so it is not news.
        if live_projection is not None and live_projection != PROJECTION_ALL:
            findings.append(Finding(
                kind=FindingKind.UNDECLARED_PROJECTION,
                severity=Severity.UNDECLARED,
                remedy=Remedy.ADOPT_DECLARATION,
                table_name=table_name,
                index_name=index_name,
                message=(
                    f"{index_name}: live projection is {_describe_projection(live)}, "
                    f"which the declaration does not model. Inherited on rebuild; "
                    f"declare it in gsi_schemas to make it authoritative"
                ),
                declared=None,
                live=dict(live),
            ))
    elif (declared_projection, declared_non_key) != (live_projection, live_non_key):
        findings.append(Finding(
            kind=FindingKind.CONFLICTING_PROJECTION,
            severity=Severity.CONFLICT,
            remedy=Remedy.REBUILD_GSI,
            table_name=table_name,
            index_name=index_name,
            message=(
                f"{index_name}: declared projection {_describe_projection(declared)} "
                f"but live is {_describe_projection(live)}; the index will be rebuilt "
                f"to match the declaration"
            ),
            declared=dict(declared),
            live=dict(live),
        ))

    return findings


def diff_schemas(
    expected: Mapping[str, Any],
    actual: Optional[Mapping[str, Any]],
) -> SchemaDiff:
    """Compare a declared schema against the live one.

    Pass `actual=None` when the table does not exist.
    """
    table_name = expected["table_name"]

    if actual is None:
        return SchemaDiff(table_name=table_name, findings=(Finding(
            kind=FindingKind.MISSING_TABLE,
            severity=Severity.MISSING,
            remedy=Remedy.CREATE_TABLE,
            table_name=table_name,
            message=f"Missing table {table_name}: declared but does not exist; it will be created",
            declared=dict(expected["key_schema"]),
        ),))

    findings: List[Finding] = []

    if expected["key_schema"] != actual["key_schema"]:
        findings.append(Finding(
            kind=FindingKind.CONFLICTING_TABLE_KEY,
            severity=Severity.CONFLICT,
            remedy=Remedy.RECREATE_TABLE,
            table_name=table_name,
            message=(
                f"{table_name}: declared primary key {expected['key_schema']} but "
                f"live is {actual['key_schema']}; only a table recreate can fix this"
            ),
            declared=dict(expected["key_schema"]),
            live=dict(actual["key_schema"]),
        ))

    expected_gsis: Mapping[str, Mapping[str, Any]] = expected.get("gsis", {})
    actual_gsis: Mapping[str, Mapping[str, Any]] = actual.get("gsis", {})

    for index_name in sorted(set(expected_gsis) - set(actual_gsis)):
        findings.append(Finding(
            kind=FindingKind.MISSING_GSI,
            severity=Severity.MISSING,
            remedy=Remedy.CREATE_GSI,
            table_name=table_name,
            index_name=index_name,
            message=f"{index_name}: declared but absent; it will be created",
            declared=dict(expected_gsis[index_name]),
        ))

    for index_name in sorted(set(actual_gsis) - set(expected_gsis)):
        findings.append(Finding(
            kind=FindingKind.UNDECLARED_GSI,
            severity=Severity.UNDECLARED,
            remedy=Remedy.DELETE_GSI,
            table_name=table_name,
            index_name=index_name,
            message=(
                f"{index_name}: live index is not declared in Python. Left as-is; "
                f"declare it to adopt it, or enable pruning to delete it"
            ),
            live=dict(actual_gsis[index_name]),
        ))

    for index_name in sorted(set(expected_gsis) & set(actual_gsis)):
        findings.extend(_diff_gsi(
            table_name=table_name,
            index_name=index_name,
            declared=expected_gsis[index_name],
            live=actual_gsis[index_name],
        ))

    # A key attribute's type cannot be altered in place, so a mismatch is a
    # table recreate. Only compare attributes both sides consider key attributes.
    expected_types: Mapping[str, str] = expected.get("attribute_types", {})
    actual_types: Mapping[str, str] = actual.get("attribute_types", {})
    for attribute in sorted(set(expected_types) & set(actual_types)):
        if expected_types[attribute] == actual_types[attribute]:
            continue
        findings.append(Finding(
            kind=FindingKind.CONFLICTING_ATTRIBUTE_TYPE,
            severity=Severity.CONFLICT,
            remedy=Remedy.RECREATE_TABLE,
            table_name=table_name,
            attribute=attribute,
            message=(
                f"{table_name}: key attribute {attribute!r} is declared "
                f"{expected_types[attribute]!r} but live is {actual_types[attribute]!r}; "
                f"only a table recreate can fix this"
            ),
            declared=expected_types[attribute],
            live=actual_types[attribute],
        ))

    # Billing mode is not declarable today -- expected_schema() hardcodes
    # PAY_PER_REQUEST -- so a mismatch is an observation, not drift.
    expected_billing = expected.get("billing_mode")
    actual_billing = actual.get("billing_mode")
    if expected_billing and actual_billing and expected_billing != actual_billing:
        findings.append(Finding(
            kind=FindingKind.UNMANAGED_BILLING_MODE,
            severity=Severity.INFO,
            remedy=Remedy.NONE,
            table_name=table_name,
            message=(
                f"{table_name}: live billing mode is {actual_billing!r} but the code "
                f"assumes {expected_billing!r}. Not managed by this tooling"
            ),
            declared=expected_billing,
            live=actual_billing,
        ))

    return SchemaDiff(table_name=table_name, findings=tuple(findings))
