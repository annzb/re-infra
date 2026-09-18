"""Report how live DynamoDB tables differ from their Python declarations.

Read-only. There is no apply and never will be: this is the tool you run
*before* deploying, to see what the sync would do and to catch indexes or
attributes somebody created in the console without updating the code.

Reading the output
------------------
`undeclared` findings are the ones that matter for this workflow. They mean the
live table has something -- an index, a sort key, a projection, an attribute --
that the code does not model. The sync leaves all of it alone, so nothing is
broken; but until it is declared, nothing enforces it either, and a rebuild
inherits rather than reproduces it. `--format python` emits the declaration.

`rebuild_gsi` / `recreate_table` findings are what the next deploy would change.
Check those are what you intend before deploying.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from typing import Any

from rc_dynamo.base_table import BaseTable
from rc_dynamo.schema.diff import Finding, FindingKind, Remedy, Severity, diff_schemas
from rc_dynamo.utils.settings import Settings

MAX_SAMPLE_WITHOUT_OPT_IN = 5000
REPORT_FORMATS = ("text", "json", "python")

# DynamoDB key attribute type -> the annotation a generated field stub should use.
_ATTRIBUTE_TYPE_HINTS = {"S": "Optional[str]", "N": "Optional[int]", "B": "Optional[bytes]"}


class SchemaReportError(RuntimeError):
    pass


def check_sample_size(sample_items: int, allow_large_sample: bool) -> None:
    if sample_items > MAX_SAMPLE_WITHOUT_OPT_IN and not allow_large_sample:
        raise SchemaReportError(
            f"--sample-items {sample_items} scans a lot of a production table. "
            f"Pass --allow-large-sample if that is intended."
        )


# ─────────────────────────── undeclared attributes ───────────────────────────


def sample_attributes(table: BaseTable[Any], limit: int) -> tuple[Counter, int]:
    """Count attribute names across a bounded sample of items.

    Also samples each GSI separately. Scan returns items in partition-hash
    order, so on a single-table design the first N items skew heavily toward
    whichever entity type hashes early; a sparse attribute written on a handful
    of rows would never appear in a plain table scan. Scanning an index reaches
    exactly the rows that carry its key.
    """
    counts: Counter = Counter()
    seen = 0

    sources: list[dict[str, Any]] = [{}]
    sources += [{"IndexName": name} for name in sorted(table.actual_schema().get("gsis", {}))]

    per_source = max(1, limit // len(sources))

    for source in sources:
        remaining = per_source
        kwargs: dict[str, Any] = dict(source)
        while remaining > 0:
            kwargs["Limit"] = min(remaining, 100)
            response = table.table.scan(**kwargs)
            items = response.get("Items", [])
            for item in items:
                counts.update(item.keys())
                seen += 1
            remaining -= len(items) or remaining
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

    return counts, seen


def undeclared_attribute_findings(table: BaseTable[Any], limit: int) -> list[Finding]:
    item_model = table.item_model
    declared = set(item_model.model_fields)
    acknowledged = set(getattr(item_model, "ignored_attributes", frozenset()))
    key_attributes = set(table.actual_schema().get("attribute_types", {}))

    counts, sampled = sample_attributes(table, limit)
    if not sampled:
        return []

    permissive = item_model.model_config.get("extra") == "allow"
    findings = []

    for attribute in sorted(set(counts) - declared - acknowledged - key_attributes):
        findings.append(
            Finding(
                kind=FindingKind.UNDECLARED_ATTRIBUTE,
                severity=Severity.UNDECLARED,
                remedy=Remedy.ADOPT_DECLARATION,
                table_name=table.table_name,
                attribute=attribute,
                message=(
                    f"{attribute!r}: present on {counts[attribute]}/{sampled} sampled items "
                    f"but not a field on {item_model.__name__} "
                    f"({'kept at runtime, extra=allow' if permissive else 'DROPPED on read'})"
                ),
                live=counts[attribute],
            )
        )

    return findings


# ────────────────────────────────── codegen ──────────────────────────────────


def python_suggestions(table: BaseTable[Any], findings: Sequence[Finding]) -> str | None:
    """Paste-ready declarations adopting whatever is live but unmodeled.

    Emits the *whole* gsi_schemas block, merged with what the model already
    declares, so pasting over the existing one is correct.
    """
    adoptable = {
        f.index_name
        for f in findings
        if f.kind
        in (
            FindingKind.UNDECLARED_SORT_KEY,
            FindingKind.UNDECLARED_PROJECTION,
            FindingKind.UNDECLARED_GSI,
        )
        and f.index_name
    }
    if not adoptable:
        return None

    item_model = table.item_model
    live = table.actual_schema()
    live_gsis = live.get("gsis", {})
    attribute_types = live.get("attribute_types", {})
    declared_fields = set(item_model.model_fields)

    merged: dict[str, dict[str, Any]] = {
        name: dict(spec) for name, spec in getattr(item_model, "gsi_schemas", {}).items()
    }
    for index_name in sorted(name for name in adoptable if name):
        gsi = live_gsis.get(index_name)
        if not gsi:
            continue
        spec: dict[str, Any] = {"partition_key": gsi.get("partition_key")}
        if gsi.get("sort_key"):
            spec["sort_key"] = gsi["sort_key"]
        if gsi.get("projection") and gsi["projection"] != "ALL":
            spec["projection"] = gsi["projection"]
            if gsi.get("non_key_attributes"):
                spec["non_key_attributes"] = sorted(gsi["non_key_attributes"])
        merged[index_name] = spec

    # Every partition_key/sort_key named in gsi_schemas must be a declared field,
    # or BaseItem.__pydantic_init_subclass__ raises at class-definition time --
    # which means importing the schema module fails and nothing starts.
    missing_fields = sorted(
        {
            name
            for spec in merged.values()
            for name in (spec.get("partition_key"), spec.get("sort_key"))
            if name and name not in declared_fields
        }
    )

    lines: list[str] = [
        f"# ── {item_model.__name__} ({table.table_name}) ──",
    ]

    if missing_fields:
        lines += [
            "#",
            "# BLOCKER: these are key attributes of a live index but are NOT fields on",
            f"# {item_model.__name__}. BaseItem rejects gsi_schemas entries naming a",
            "# non-field at class-definition time, so adding the block below WITHOUT",
            "# these fields makes importing the schema module raise -- every Lambda would",
            "# fail to start. Apply BOTH parts in the same commit.",
            "#",
            "# 1) add these fields:",
        ]
        for name in missing_fields:
            hint = _ATTRIBUTE_TYPE_HINTS.get(attribute_types.get(name, "S"), "Optional[str]")
            lines.append(f"    {name}: {hint} = None")
        lines += ["#", "# 2) then the declaration:"]

    lines.append("    gsi_schemas: ClassVar[Mapping[str, Mapping[str, Any]]] = {")
    for index_name in sorted(merged):
        spec = merged[index_name]
        lines.append(f"        {index_name!r}: {{")
        for key in ("partition_key", "sort_key", "projection", "non_key_attributes"):
            if key in spec and spec[key] is not None:
                lines.append(f"            {key!r}: {spec[key]!r},")
        lines.append("        },")
    lines.append("    }")

    return "\n".join(lines)


# ────────────────────────────────── output ──────────────────────────────────


def render_text(table_name: str, findings: Sequence[Finding]) -> str:
    if not findings:
        return f"{table_name}: OK -- live schema matches the declaration"

    groups: dict[str, list[Finding]] = {
        "Will change on next deploy": [],
        "Undeclared (left alone)": [],
        "Info": [],
    }
    for finding in findings:
        if finding.remedy is Remedy.ADOPT_DECLARATION:
            groups["Undeclared (left alone)"].append(finding)
        elif finding.severity is Severity.INFO:
            groups["Info"].append(finding)
        else:
            groups["Will change on next deploy"].append(finding)

    lines = [f"{table_name}:"]
    for heading, group in groups.items():
        if not group:
            continue
        lines.append(f"  {heading}:")
        for finding in group:
            lines.append(f"    [{finding.remedy}] {finding.message}")
    return "\n".join(lines)


def build_report(
    tables: Sequence[BaseTable[Any]],
    *,
    fmt: str = "text",
    sample_items: int = 0,
    settings: Settings | None = None,
) -> tuple[str, bool]:
    """Render the report. Returns ``(output, any_findings)``."""
    if fmt not in REPORT_FORMATS:
        raise SchemaReportError(f"Unknown format {fmt!r}; expected one of {REPORT_FORMATS}")
    settings = settings if settings is not None else Settings.from_env()

    lines: list[str] = []
    if fmt == "text":
        lines += [
            f"Tables: {', '.join(t.table_name for t in tables)}",
            f"Permissions: DYNAMO_PRUNE_UNDECLARED={str(settings.prune_undeclared).lower()}, "
            f"DYNAMO_ALLOW_TABLE_RECREATE={str(settings.allow_table_recreate).lower()}",
            "Not compared: TTL, streams, PITR, tags, autoscaling, local secondary indexes",
            "",
        ]

    payload: list[dict[str, Any]] = []
    suggestions: list[str] = []
    any_findings = False

    for table in tables:
        actual = table.actual_schema() if table.exists() else None
        diff = diff_schemas(table.expected_schema(), actual)
        findings = list(diff.findings)

        if actual is not None and sample_items:
            findings.extend(undeclared_attribute_findings(table, sample_items))

        if findings:
            any_findings = True

        if fmt == "text":
            lines += [render_text(table.table_name, findings), ""]
        elif fmt == "json":
            payload.append(
                {"table_name": table.table_name, "findings": [f.to_json() for f in findings]}
            )
        elif actual is not None:
            block = python_suggestions(table, findings)
            if block:
                suggestions.append(block)

    if fmt == "json":
        lines.append(json.dumps(payload, indent=2, sort_keys=True))
    elif fmt == "python":
        lines.append(
            "\n\n".join(suggestions)
            if suggestions
            else "# Nothing to adopt: every live index is fully declared."
        )

    return "\n".join(lines), any_findings
