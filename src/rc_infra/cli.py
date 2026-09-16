"""rc-infra: validate the environment catalog, plan changes, and apply them.

    rc-infra validate                  catalog only, no AWS access
    rc-infra plan [--format FORMAT]    read AWS and show what apply would do
    rc-infra apply [--yes]             carry out the plan (prints it and stops without --yes)

Exit codes: 0 success, 1 invalid catalog / blocked plan / failed apply, 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from rc_infra import aws as aws_module
from rc_infra.apply import ApplyRefused, apply_plan
from rc_infra.catalog import DEFAULT_CATALOG_PATH, Catalog, CatalogError, load_catalog
from rc_infra.planner import build_plan, render_markdown, render_text

CFN_ROLE_ENV = "RC_INFRA_CFN_ROLE_ARN"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
    except CatalogError as exc:
        print(f"Catalog {args.catalog} is invalid:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if args.command == "validate":
        print(f"Catalog {args.catalog} is valid: {len(catalog.environments)} environments.")
        return 0

    account_error = _check_account(catalog)
    if account_error:
        print(account_error, file=sys.stderr)
        return 1

    aws = aws_module.connect(catalog.region, args.cfn_role_arn)
    plan = build_plan(catalog, aws)

    if args.command == "plan":
        rendered = {"text": render_text, "markdown": render_markdown}.get(args.format)
        print(rendered(plan) if rendered else plan.to_json())
        return 1 if plan.blocked else 0

    print(render_text(plan))
    if not args.yes:
        print("\nDry run. Re-run with --yes to apply.")
        return 1 if plan.blocked else 0
    try:
        result = apply_plan(plan, catalog, aws)
    except ApplyRefused as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    if result.failed:
        print("\nFailed:", file=sys.stderr)
        for failure in result.failed:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("\nApply complete.")
    return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)

    aws_options = argparse.ArgumentParser(add_help=False)
    aws_options.add_argument(
        "--cfn-role-arn",
        default=os.environ.get(CFN_ROLE_ENV) or None,
        help=f"role CloudFormation assumes (default: ${CFN_ROLE_ENV})",
    )

    parser = argparse.ArgumentParser(prog="rc-infra", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", parents=[common], help="validate the catalog")
    plan = commands.add_parser("plan", parents=[common, aws_options], help="show planned changes")
    plan.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    apply = commands.add_parser("apply", parents=[common, aws_options], help="apply the plan")
    apply.add_argument("--yes", action="store_true", help="actually apply")
    return parser


def _check_account(catalog: Catalog) -> str | None:
    account = aws_module.caller_account(catalog.region)
    if account != catalog.account_id:
        return (
            f"Refusing to continue: credentials are for account {account}, "
            f"but the catalog targets {catalog.account_id}."
        )
    return None


if __name__ == "__main__":
    raise SystemExit(main())
