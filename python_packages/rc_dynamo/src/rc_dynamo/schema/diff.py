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
live."** It applies uniformly to a GSI's `sort_key` and its `projection`. Table
options follow the same rule through an explicit sentinel, `UNMANAGED`
(see `rc_dynamo.schema.options`), because `None` is a meaningful value there
(TTL / streams disabled). An unmanaged option is never compared and never
turned off.

The `BaseItem.gsis` shorthand can only express a HASH key, so every index
declared through it reports `sort_key=None` and `projection=None`. Treating that
as "the live index must have no sort key" would flag most of the live estate as
drift; treating it as "unmodeled" lets declarations be adopted incrementally.
Such gaps are still reported -- as `UNDECLARED_*` findings, whose only remedy is
a human editing the Python -- so "no conflicts" never silently means "we didn't
look".

What is compared
----------------
* Primary key, key attribute types, GSIs (keys, projection incl. INCLUDE).
* Table options, when declared: billing mode, table class, deletion protection,
  point-in-time recovery, TTL attribute, stream view type. Drift is a
  `CONFLICTING_TABLE_OPTION` fixed in place (`UPDATE_OPTIONS`). Weakening a
  protection -- disabling PITR or deletion protection -- is `DISABLE_PROTECTION`
  and needs `ALLOW_PROTECTION_DOWNGRADE`. None of these needs a new table.

Unsupported live features
-------------------------
Things this model cannot express, so a recreate could not reproduce them, are
reported as `UNSUPPORTED_LIVE_FEATURE`: **local secondary indexes**, KMS
encryption (AWS-managed or customer key), global-table replicas, on-demand
throughput limits, and -- unless declared -- a stream, the infrequent-access table
class, or provisioned billing. They never block a deploy that leaves the table in
place, but any table recreate is refused (`RECREATE_REFUSED`) while one exists,
and while deletion protection is enabled live. LSIs deserve the loudest warning:
they cannot be added after a table is created, so a recreate destroys them
unrecoverably.

Not inspected at all
--------------------
Tags beyond the sync's ownership tags, autoscaling policies, contributor
insights, AWS Backup plans and warm throughput. Kinesis streaming destinations
and resource policies are not part of the schema dicts either; the sync checks
them live just before a recreate and refuses if either exists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rc_dynamo.schema.options import (
    BILLING_PROVISIONED,
    OPTION_NAMES,
    TABLE_CLASS_INFREQUENT_ACCESS,
    UNMANAGED,
    Unmanaged,
)

# Attribute-type codes DynamoDB allows on a key attribute.
KEY_ATTRIBUTE_TYPES = ("S", "N", "B")

PROJECTION_ALL = "ALL"
PROJECTION_KEYS_ONLY = "KEYS_ONLY"
PROJECTION_INCLUDE = "INCLUDE"
PROJECTION_TYPES = (PROJECTION_ALL, PROJECTION_KEYS_ONLY, PROJECTION_INCLUDE)


class Severity(StrEnum):
    """What kind of difference this is -- descriptive, for grouping a report."""

    CONFLICT = "conflict"  # declared and live disagree about a modeled thing
    MISSING = "missing"  # declared, absent live
    UNDECLARED = "undeclared"  # live, not modeled in Python
    UNSUPPORTED = "unsupported"  # live, and not even expressible in Python
    INFO = "info"  # observation this tooling does not manage


class FindingKind(StrEnum):
    MISSING_TABLE = "missing_table"
    MISSING_GSI = "missing_gsi"

    UNDECLARED_GSI = "undeclared_gsi"
    UNDECLARED_SORT_KEY = "undeclared_sort_key"
    UNDECLARED_PROJECTION = "undeclared_projection"
    UNDECLARED_ATTRIBUTE = "undeclared_attribute"
    UNDECLARED_TABLE_OPTION = "undeclared_table_option"

    CONFLICTING_TABLE_KEY = "conflicting_table_key"
    CONFLICTING_GSI_KEY = "conflicting_gsi_key"
    CONFLICTING_PROJECTION = "conflicting_projection"
    CONFLICTING_ATTRIBUTE_TYPE = "conflicting_attribute_type"
    CONFLICTING_TABLE_OPTION = "conflicting_table_option"

    UNSUPPORTED_LIVE_FEATURE = "unsupported_live_feature"
    RECREATE_REFUSED = "recreate_refused"

    # No longer emitted: billing mode is now a TableOptions field, so a mismatch
    # is CONFLICTING_TABLE_OPTION (declared) or UNSUPPORTED_LIVE_FEATURE (not).
    # Kept so code that names it keeps importing.
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
    UPDATE_OPTIONS = "update_options"
    DISABLE_PROTECTION = "disable_protection"
    ADOPT_DECLARATION = "adopt_declaration"
    NONE = "none"


class Permission(StrEnum):
    PRUNE_UNDECLARED = "prune_undeclared"
    ALLOW_TABLE_RECREATE = "allow_table_recreate"
    ALLOW_PROTECTION_DOWNGRADE = "allow_protection_downgrade"


# Creating what the code declares, and reconciling an index the code already
# declares, are the deploy's job and need no permission -- that is what
# "declarations are the source of truth" means. Rebuilding a GSI deletes and
# recreates one index; it cannot lose a row, because a GSI holds only derived
# data. Destroying undeclared work, or a table's rows, is opt-in.
#
# UPDATE_OPTIONS applies a declared table option in place (TTL, enabling PITR or
# deletion protection, billing mode, table class, streams) and is ungated for the
# same reason. DISABLE_PROTECTION is the one in-place option change that is not:
# turning PITR off discards the restore window irrecoverably, and turning
# deletion protection off removes the only guard against a DeleteTable. A
# declaration flipping either to False must be confirmed by an operator.
#
# ADOPT_DECLARATION maps to None because it is never automatable: the only fix
# is a human editing the Python. Those findings are reported and skipped.
REQUIRED_PERMISSION: Mapping[Remedy, Permission | None] = {
    Remedy.CREATE_TABLE: None,
    Remedy.CREATE_GSI: None,
    Remedy.REBUILD_GSI: None,
    Remedy.DELETE_GSI: Permission.PRUNE_UNDECLARED,
    Remedy.RECREATE_TABLE: Permission.ALLOW_TABLE_RECREATE,
    Remedy.UPDATE_OPTIONS: None,
    Remedy.DISABLE_PROTECTION: Permission.ALLOW_PROTECTION_DOWNGRADE,
    Remedy.ADOPT_DECLARATION: None,
    Remedy.NONE: None,
}

# Remedies the deploy executes. Everything else is report-only.
AUTOMATABLE_REMEDIES = frozenset(
    {
        Remedy.CREATE_TABLE,
        Remedy.CREATE_GSI,
        Remedy.REBUILD_GSI,
        Remedy.DELETE_GSI,
        Remedy.RECREATE_TABLE,
        Remedy.UPDATE_OPTIONS,
        Remedy.DISABLE_PROTECTION,
    }
)

# Remedies that replace the physical table. Everything else automatable is in place.
NEW_TABLE_REMEDIES = frozenset({Remedy.CREATE_TABLE, Remedy.RECREATE_TABLE})


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    severity: Severity
    remedy: Remedy
    table_name: str
    message: str
    index_name: str | None = None
    attribute: str | None = None
    declared: Any = None
    live: Any = None

    @property
    def required_permission(self) -> Permission | None:
        return REQUIRED_PERMISSION[self.remedy]

    @property
    def is_automatable(self) -> bool:
        return self.remedy in AUTOMATABLE_REMEDIES

    @property
    def needs_new_table(self) -> bool:
        """Whether resolving this finding means a new physical table."""
        return self.remedy in NEW_TABLE_REMEDIES

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
    findings: tuple[Finding, ...]

    @property
    def is_clean(self) -> bool:
        return not self.findings

    def of_severity(self, *severities: Severity) -> tuple[Finding, ...]:
        wanted = set(severities)
        return tuple(f for f in self.findings if f.severity in wanted)

    def with_remedy(self, *remedies: Remedy) -> tuple[Finding, ...]:
        wanted = set(remedies)
        return tuple(f for f in self.findings if f.remedy in wanted)

    def actionable(self, granted: Mapping[Permission, bool]) -> tuple[Finding, ...]:
        """Findings the deploy can and may execute, given the granted permissions."""
        return tuple(
            f
            for f in self.findings
            if f.is_automatable
            and (f.required_permission is None or granted.get(f.required_permission, False))
        )

    def blocked(self, granted: Mapping[Permission, bool]) -> tuple[Finding, ...]:
        """Conflicts the deploy must resolve but is not permitted to. These fail the run.

        Only CONFLICT findings can block. An UNDECLARED finding without its
        permission is not a failure -- leaving undeclared work alone is the
        intended behaviour, so it is simply skipped.
        """
        return tuple(
            f
            for f in self.findings
            if f.severity is Severity.CONFLICT
            and f.is_automatable
            and f.required_permission is not None
            and not granted.get(f.required_permission, False)
        )

    def unfixable(self) -> tuple[Finding, ...]:
        """Conflicts no permission lets the deploy resolve. These fail the run too.

        A declared option the sync cannot apply (switching to PROVISIONED), or a
        recreate refused because the live table has something it would destroy.
        A human has to change AWS or the declaration.
        """
        return tuple(
            f
            for f in self.findings
            if f.severity is Severity.CONFLICT
            and not f.is_automatable
            and f.remedy is not Remedy.ADOPT_DECLARATION
        )

    def requires_action(self, granted: Mapping[Permission, bool]) -> bool:
        """Whether the live schema differs in a way the sync cares about.

        Drives the dry-run exit code. Undeclared and unsupported elements are
        excluded: they are reported, but the sync will never act on them, so a
        deploy that leaves them alone has nothing to do.
        """
        return bool(self.actionable(granted) or self.blocked(granted) or self.unfixable())

    def to_json(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "findings": [f.to_json() for f in self.findings],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Unmanaged):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Mapping):
        return {key: _jsonable(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(inner) for inner in value]
    return value


def normalize_non_key_attributes(value: Any) -> frozenset[str] | None:
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
    live_gsi: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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

    resolved: dict[str, Any] = {"ProjectionType": projection}
    if projection == PROJECTION_INCLUDE and non_key:
        resolved["NonKeyAttributes"] = sorted(non_key)
    return resolved


def _diff_gsi(
    table_name: str,
    index_name: str,
    declared: Mapping[str, Any],
    live: Mapping[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []

    declared_pk = declared.get("partition_key")
    live_pk = live.get("partition_key")
    declared_sk = declared.get("sort_key")
    live_sk = live.get("sort_key")

    # A partition-key change is always a conflict; there is nothing to "adopt".
    if declared_pk != live_pk:
        findings.append(
            Finding(
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
            )
        )
    elif declared_sk is None and live_sk is not None:
        # Unmodeled sort key: the shorthand cannot express it. Report, never touch.
        findings.append(
            Finding(
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
            )
        )
    elif declared_sk != live_sk:
        findings.append(
            Finding(
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
            )
        )

    declared_projection = declared.get("projection")
    live_projection = live.get("projection")
    declared_non_key = normalize_non_key_attributes(declared.get("non_key_attributes"))
    live_non_key = normalize_non_key_attributes(live.get("non_key_attributes"))

    if declared_projection is None:
        # Only worth reporting when the live projection is something a future
        # rebuild could lose. ALL is the create-time default, so it is not news.
        if live_projection is not None and live_projection != PROJECTION_ALL:
            findings.append(
                Finding(
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
                )
            )
    elif (declared_projection, declared_non_key) != (live_projection, live_non_key):
        findings.append(
            Finding(
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
            )
        )

    return findings


# ─────────────────────────────── table options ───────────────────────────────

_OPTION_LABELS: Mapping[str, str] = {
    "billing_mode": "billing mode",
    "table_class": "table class",
    "deletion_protection": "deletion protection",
    "point_in_time_recovery": "point-in-time recovery",
    "ttl_attribute": "TTL",
    "stream_view_type": "stream",
}

# The options whose True -> False transition weakens a protection.
_PROTECTION_OPTIONS = frozenset({"deletion_protection", "point_in_time_recovery"})

# What CreateTable produces when an option is not set. An unmanaged option whose
# live value differs from this is worth reporting as UNDECLARED: it is inherited
# on recreate, but nothing enforces it. Billing, class and streams are absent on
# purpose -- a non-default live value there is UNSUPPORTED, not merely undeclared.
_CREATE_DEFAULTS: Mapping[str, Any] = {
    "deletion_protection": False,
    "point_in_time_recovery": False,
    "ttl_attribute": None,
}


def _describe_option(name: str, value: Any) -> str:
    if value is UNMANAGED:
        return "unmanaged"
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    if value is None:
        return "disabled"
    if name == "ttl_attribute":
        return f"enabled on {value!r}"
    return str(value)


def _option_remedy(name: str, declared: Any, live: Any) -> Remedy:
    if name in _PROTECTION_OPTIONS and declared is False and live is True:
        return Remedy.DISABLE_PROTECTION
    if name == "billing_mode" and declared == BILLING_PROVISIONED:
        # Provisioned capacity (and its autoscaling) is not modeled, so there is
        # nothing to switch *to*. Declaring PROVISIONED only acknowledges it.
        return Remedy.NONE
    return Remedy.UPDATE_OPTIONS


def _option_consequence(name: str, remedy: Remedy, live: Any) -> str:
    if remedy is Remedy.DISABLE_PROTECTION:
        return "disabling a protection must be explicitly allowed"
    if remedy is Remedy.NONE:
        return (
            "the sync cannot switch a table to PROVISIONED because capacity is not "
            "modeled; change it in AWS, or declare PAY_PER_REQUEST"
        )
    if name == "ttl_attribute" and isinstance(live, str):
        return (
            "it will be changed in place. Moving TTL to another attribute disables it "
            "first, and DynamoDB allows one TTL change per hour, so this can take two "
            "deploys"
        )
    return "it will be changed in place"


def _diff_options(
    table_name: str,
    declared_options: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[Finding]:
    """Declared-vs-live drift for every option the declaration manages.

    An option that is UNMANAGED in the declaration -- or unknown live, as in a
    hand-built schema dict -- is not compared at all.
    """
    live_options: Mapping[str, Any] = actual.get("options", {})
    findings: list[Finding] = []
    for name in OPTION_NAMES:
        declared = declared_options.get(name, UNMANAGED)
        live = live_options.get(name, UNMANAGED)
        if live is UNMANAGED:
            continue
        if declared is UNMANAGED:
            if name in _CREATE_DEFAULTS and live != _CREATE_DEFAULTS[name]:
                findings.append(
                    Finding(
                        kind=FindingKind.UNDECLARED_TABLE_OPTION,
                        severity=Severity.UNDECLARED,
                        remedy=Remedy.ADOPT_DECLARATION,
                        table_name=table_name,
                        attribute=name,
                        message=(
                            f"{table_name}: live {_OPTION_LABELS[name]} is "
                            f"{_describe_option(name, live)}, which table_options does not "
                            f"model. Left as-is and inherited on recreate; declare {name} "
                            f"to make it authoritative"
                        ),
                        live=live,
                    )
                )
            continue
        if declared == live:
            continue
        remedy = _option_remedy(name, declared, live)
        findings.append(
            Finding(
                kind=FindingKind.CONFLICTING_TABLE_OPTION,
                severity=Severity.CONFLICT,
                remedy=remedy,
                table_name=table_name,
                attribute=name,
                message=(
                    f"{table_name}: declared {_OPTION_LABELS[name]} "
                    f"{_describe_option(name, declared)} but live is "
                    f"{_describe_option(name, live)}; "
                    f"{_option_consequence(name, remedy, live)}"
                ),
                declared=declared,
                live=live,
            )
        )
    return findings


def _unsupported(
    table_name: str,
    attribute: str,
    live: Any,
    message: str,
    index_name: str | None = None,
) -> Finding:
    return Finding(
        kind=FindingKind.UNSUPPORTED_LIVE_FEATURE,
        severity=Severity.UNSUPPORTED,
        remedy=Remedy.NONE,
        table_name=table_name,
        index_name=index_name,
        attribute=attribute,
        message=message,
        live=live,
    )


def _unsupported_features(
    table_name: str,
    declared_options: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[Finding]:
    """Live features this tooling cannot reproduce. Each one makes a recreate refuse."""
    features: Mapping[str, Any] = actual.get("features", {})
    live_options: Mapping[str, Any] = actual.get("options", {})
    findings: list[Finding] = []
    left_alone = "Left as-is, but a table recreate is refused while it exists"

    lsis: Mapping[str, Any] = features.get("local_secondary_indexes") or {}
    for index_name in sorted(lsis):
        findings.append(
            _unsupported(
                table_name,
                "local_secondary_index",
                dict(lsis[index_name]),
                (
                    f"{index_name}: live local secondary index, which cannot be modeled "
                    f"here. An LSI cannot be added after a table is created, so a "
                    f"recreate would destroy it unrecoverably. {left_alone}"
                ),
                index_name=index_name,
            )
        )

    def undeclared_option(name: str, live_value: Any, what: str) -> None:
        if declared_options.get(name, UNMANAGED) is not UNMANAGED:
            return  # declared: compared by _diff_options instead
        findings.append(
            _unsupported(
                table_name,
                name,
                live_value,
                (
                    f"{table_name}: live {what}, which the declaration does not model. "
                    f"{left_alone}; declare {name} in table_options to adopt it"
                ),
            )
        )

    stream = live_options.get("stream_view_type", UNMANAGED)
    if stream is not UNMANAGED and stream is not None:
        undeclared_option("stream_view_type", stream, f"stream ({stream})")
    if live_options.get("table_class") == TABLE_CLASS_INFREQUENT_ACCESS:
        undeclared_option(
            "table_class", TABLE_CLASS_INFREQUENT_ACCESS, "table class STANDARD_INFREQUENT_ACCESS"
        )
    if live_options.get("billing_mode") == BILLING_PROVISIONED:
        undeclared_option("billing_mode", BILLING_PROVISIONED, "PROVISIONED billing")

    sse = features.get("sse")
    if sse:
        findings.append(
            _unsupported(
                table_name,
                "sse",
                dict(sse),
                (
                    f"{table_name}: live KMS encryption ({sse.get('kms_key') or 'AWS managed'}), "
                    f"which cannot be modeled here. {left_alone}"
                ),
            )
        )

    replicas = features.get("replicas") or ()
    if replicas:
        findings.append(
            _unsupported(
                table_name,
                "replicas",
                sorted(replicas),
                (
                    f"{table_name}: global-table replicas in {', '.join(sorted(replicas))}, "
                    f"which cannot be modeled here. {left_alone}"
                ),
            )
        )

    on_demand = features.get("on_demand_throughput")
    if on_demand:
        findings.append(
            _unsupported(
                table_name,
                "on_demand_throughput",
                dict(on_demand),
                (
                    f"{table_name}: live on-demand throughput limits {dict(on_demand)}, "
                    f"which cannot be modeled here. {left_alone}"
                ),
            )
        )

    return findings


_FEATURE_LABELS: Mapping[str, str] = {
    "local_secondary_index": "local secondary index",
    "stream_view_type": "an undeclared stream",
    "table_class": "an undeclared STANDARD_INFREQUENT_ACCESS table class",
    "billing_mode": "undeclared PROVISIONED billing",
    "sse": "KMS encryption",
    "replicas": "global-table replicas",
    "on_demand_throughput": "on-demand throughput limits",
}


def recreate_blockers(
    unsupported: Sequence[Finding],
    actual: Mapping[str, Any],
    declared_options: Mapping[str, Any] | None = None,
) -> list[str]:
    """Why a recreate of this live table must not run, as human-readable reasons."""
    reasons = []
    if (declared_options or {}).get("billing_mode") == BILLING_PROVISIONED:
        # The new table could not be created: capacity is not modeled.
        reasons.append("declared PROVISIONED billing, which this tooling cannot create")
    for finding in unsupported:
        label = _FEATURE_LABELS.get(str(finding.attribute), str(finding.attribute))
        if finding.index_name:
            label = f"{label} {finding.index_name!r}"
        reasons.append(label)
    if actual.get("options", {}).get("deletion_protection") is True:
        reasons.append("deletion protection enabled")
    return reasons


def _recreate_refusal(
    table_name: str,
    unsupported: Sequence[Finding],
    actual: Mapping[str, Any],
    declared_options: Mapping[str, Any],
) -> Finding | None:
    reasons = recreate_blockers(unsupported, actual, declared_options)
    if not reasons:
        return None
    return Finding(
        kind=FindingKind.RECREATE_REFUSED,
        severity=Severity.CONFLICT,
        remedy=Remedy.NONE,
        table_name=table_name,
        message=(
            f"{table_name}: a table recreate is needed but refused: the live table has "
            f"{', '.join(reasons)}. A recreate would destroy what this tooling cannot "
            f"reproduce, and a protected table is never deleted. Resolve these in AWS "
            f"first, or declare the stream / table class / billing mode"
        ),
        live=reasons,
    )


def diff_schemas(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any] | None,
) -> SchemaDiff:
    """Compare a declared schema against the live one.

    Pass `actual=None` when the table does not exist.
    """
    table_name = expected["table_name"]

    if actual is None:
        return SchemaDiff(
            table_name=table_name,
            findings=(
                Finding(
                    kind=FindingKind.MISSING_TABLE,
                    severity=Severity.MISSING,
                    remedy=Remedy.CREATE_TABLE,
                    table_name=table_name,
                    message=(
                        f"Missing table {table_name}: declared but does not exist; "
                        "it will be created"
                    ),
                    declared=dict(expected["key_schema"]),
                ),
            ),
        )

    findings: list[Finding] = []

    if expected["key_schema"] != actual["key_schema"]:
        findings.append(
            Finding(
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
            )
        )

    expected_gsis: Mapping[str, Mapping[str, Any]] = expected.get("gsis", {})
    actual_gsis: Mapping[str, Mapping[str, Any]] = actual.get("gsis", {})

    for index_name in sorted(set(expected_gsis) - set(actual_gsis)):
        findings.append(
            Finding(
                kind=FindingKind.MISSING_GSI,
                severity=Severity.MISSING,
                remedy=Remedy.CREATE_GSI,
                table_name=table_name,
                index_name=index_name,
                message=f"{index_name}: declared but absent; it will be created",
                declared=dict(expected_gsis[index_name]),
            )
        )

    for index_name in sorted(set(actual_gsis) - set(expected_gsis)):
        findings.append(
            Finding(
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
            )
        )

    for index_name in sorted(set(expected_gsis) & set(actual_gsis)):
        findings.extend(
            _diff_gsi(
                table_name=table_name,
                index_name=index_name,
                declared=expected_gsis[index_name],
                live=actual_gsis[index_name],
            )
        )

    # A key attribute's type cannot be altered in place, so a mismatch is a
    # table recreate. Only compare attributes both sides consider key attributes.
    expected_types: Mapping[str, str] = expected.get("attribute_types", {})
    actual_types: Mapping[str, str] = actual.get("attribute_types", {})
    for attribute in sorted(set(expected_types) & set(actual_types)):
        if expected_types[attribute] == actual_types[attribute]:
            continue
        findings.append(
            Finding(
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
            )
        )

    findings.extend(_diff_options(table_name, expected.get("options", {}), actual))
    unsupported = _unsupported_features(table_name, expected.get("options", {}), actual)
    findings.extend(unsupported)

    recreates = [f for f in findings if f.remedy is Remedy.RECREATE_TABLE]
    if recreates:
        refusal = _recreate_refusal(table_name, unsupported, actual, expected.get("options", {}))
        if refusal is not None:
            findings.append(refusal)

    return SchemaDiff(table_name=table_name, findings=tuple(findings))
