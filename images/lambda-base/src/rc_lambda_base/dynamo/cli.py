"""Console entry points: ``rc-dynamo-sync`` and ``rc-dynamo-report``.

Exit codes (both commands):
  0  clean / success
  1  error: blocked change, difference left after --apply, bad schema module,
     missing dump bucket, invalid settings
  2  usage error
  3  differences found (sync dry run: pending schema changes; report: findings)
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from rc_lambda_base.dynamo.schema_report import (
    MAX_SAMPLE_WITHOUT_OPT_IN,
    REPORT_FORMATS,
    SchemaReportError,
    build_report,
    check_sample_size,
)
from rc_lambda_base.dynamo.schema_sync import (
    SchemaSyncError,
    load_schema_tables,
    managed_tags,
    select_tables,
    sync_tables,
)
from rc_lambda_base.settings import Settings, SettingsError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHANGES = 3

SYNC_DESCRIPTION = """\
Synchronize DynamoDB tables with their Python declarations.

Dry run by default; --apply mutates. The schema module must export
TABLES: Mapping[str, BaseTable].

Environment:
  DYNAMO_SCHEMA_MODULE, DYNAMO_SCHEMA_TABLES, DYNAMO_SCHEMA_DUMP_BUCKET,
  RC_ENVIRONMENT                     defaults for the matching flags
  DYNAMO_PRUNE_UNDECLARED=false      delete live GSIs not declared in Python
  DYNAMO_ALLOW_TABLE_RECREATE=false  dump/drop/recreate/restore on key changes
  DYNAMO_SCHEMA_POLL_SECONDS=10, DYNAMO_SCHEMA_WAIT_TIMEOUT_SECONDS=3600
  AWS_REGION, AWS_ENDPOINT_URL
"""

REPORT_DESCRIPTION = """\
Report how live DynamoDB tables differ from their Python declarations. Read-only.

  rc-dynamo-report --schema-module app.schema                  # what drifted?
  rc-dynamo-report --schema-module app.schema --format python  # adopt what is live
  rc-dynamo-report --schema-module app.schema --sample-items 500
"""


def _env(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def parse_tag(value: str) -> tuple[str, str]:
    key, separator, tag_value = value.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {value!r}")
    return key.strip(), tag_value


def _schema_module_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--schema-module",
        default=_env("DYNAMO_SCHEMA_MODULE"),
        help="Module exporting TABLES: Mapping[str, BaseTable]. Defaults to DYNAMO_SCHEMA_MODULE.",
    )


def build_sync_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rc-dynamo-sync",
        description=SYNC_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply schema changes. Without this, only report diffs.",
    )
    _schema_module_argument(parser)
    parser.add_argument(
        "--tables",
        default=_env("DYNAMO_SCHEMA_TABLES") or "all",
        help="Comma-separated TABLES keys, or 'all'. Defaults to DYNAMO_SCHEMA_TABLES or 'all'.",
    )
    parser.add_argument(
        "--dump-bucket",
        default=_env("DYNAMO_SCHEMA_DUMP_BUCKET"),
        help=(
            "Existing S3 bucket for table dumps during an allowed recreate; never created. "
            "Defaults to DYNAMO_SCHEMA_DUMP_BUCKET."
        ),
    )
    parser.add_argument(
        "--environment",
        default=_env("RC_ENVIRONMENT"),
        help=(
            "Environment written to the tables' Environment tag. Required with "
            "--apply. Defaults to RC_ENVIRONMENT."
        ),
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        type=parse_tag,
        metavar="KEY=VALUE",
        help="Extra tag for managed tables, e.g. Repository=retribalize-core. Repeatable.",
    )
    return parser


def sync_main(argv: Sequence[str] | None = None) -> int:
    parser = build_sync_parser()
    args = parser.parse_args(argv)

    if not args.schema_module:
        parser.error("--schema-module (or DYNAMO_SCHEMA_MODULE) is required")
    if args.apply and not args.environment:
        parser.error(
            "--environment (or RC_ENVIRONMENT) is required with --apply; "
            "it is written to table tags"
        )
    if args.tag and not args.environment:
        parser.error("--tag requires --environment (or RC_ENVIRONMENT)")

    tags = None
    if args.environment:
        try:
            tags = managed_tags(args.environment, dict(args.tag))
        except SchemaSyncError as exc:
            parser.error(str(exc))

    try:
        settings = Settings.from_env()
        tables = select_tables(
            load_schema_tables(args.schema_module),
            args.tables,
            module_name=args.schema_module,
        )
        any_diff = sync_tables(
            tables,
            apply=args.apply,
            settings=settings,
            dump_bucket=args.dump_bucket,
            tags=tags,
        )
    except (SchemaSyncError, SettingsError) as exc:
        print(f"[schema-sync] ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not args.apply and any_diff:
        return EXIT_CHANGES
    return EXIT_OK


def build_report_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rc-dynamo-report",
        description=REPORT_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _schema_module_argument(parser)
    parser.add_argument("--tables", default="all", help="Comma-separated TABLES keys, or 'all'.")
    parser.add_argument("--format", choices=REPORT_FORMATS, default="text")
    parser.add_argument(
        "--sample-items",
        type=int,
        default=0,
        help=(
            "Sample this many items per table to find attributes no model declares. "
            "Off by default: a scan is partition-ordered, so a small sample that finds "
            "nothing is not evidence there is nothing."
        ),
    )
    parser.add_argument(
        "--allow-large-sample",
        action="store_true",
        help=f"Permit --sample-items above {MAX_SAMPLE_WITHOUT_OPT_IN}.",
    )
    return parser


def report_main(argv: Sequence[str] | None = None) -> int:
    parser = build_report_parser()
    args = parser.parse_args(argv)

    if not args.schema_module:
        parser.error("--schema-module (or DYNAMO_SCHEMA_MODULE) is required")
    if args.sample_items < 0:
        parser.error("--sample-items must not be negative")

    try:
        check_sample_size(args.sample_items, args.allow_large_sample)
        settings = Settings.from_env()
        tables = select_tables(
            load_schema_tables(args.schema_module),
            args.tables,
            module_name=args.schema_module,
        )
        output, any_findings = build_report(
            tables,
            fmt=args.format,
            sample_items=args.sample_items,
            settings=settings,
        )
    except (SchemaReportError, SchemaSyncError, SettingsError) as exc:
        print(f"[schema-report] ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(output)
    return EXIT_CHANGES if any_findings else EXIT_OK
