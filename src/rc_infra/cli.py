"""rc-infra: validate the environment config, apply it, and report on what it created.

    rc-infra validate                                   config only, no AWS access
    rc-infra apply [--yes]                              show what AWS is missing, then carry it out with --yes
    rc-infra status [--environment X] [--format F]      which durable buckets and tables hold data
    rc-infra outputs [--environment X]                  the deployment contract (JSON) from stack outputs

Exit codes: 0 success, 1 invalid config / blocked plan / failed apply / missing outputs, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from rc_infra import aws as aws_module
from rc_infra.apply import ApplyRefused, apply_plan
from rc_infra.aws import Aws
from rc_infra.env_config import DEFAULT_ENVS_PATH, EnvConfig, EnvConfigError, Environment, load_env_config
from rc_infra.outputs import OutputsUnavailable, environment_contract
from rc_infra.planner import build_plan, render_text
from rc_infra.status import environment_status
from rc_infra.status import render_json as render_status_json
from rc_infra.status import render_text as render_status_text


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = load_env_config(args.config)
    except EnvConfigError as exc:
        print(f"{args.config} is invalid:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if args.command == "validate":
        print(f"{args.config} is valid: {len(config.environments)} environments.")
        return 0

    selected = list(config.environments)
    if getattr(args, "environment", None):
        if args.environment not in config.names:
            parser.error(f"unknown environment {args.environment!r}; declared: {', '.join(sorted(config.names))}")
        selected = [config.get(args.environment)]

    account_error = _check_account(config)
    if account_error:
        print(account_error, file=sys.stderr)
        return 1
    aws = aws_module.connect(config.region)

    if args.command == "status":
        statuses = [s for env in selected for s in environment_status(env, aws)]
        print(render_status_json(statuses) if args.format == "json" else render_status_text(statuses))
        return 0
    if args.command == "outputs":
        try:
            contracts = [environment_contract(env, aws) for env in selected]
        except OutputsUnavailable as exc:
            print(f"Outputs unavailable: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(contracts[0] if args.environment else contracts, indent=2))
        return 0
    return _apply(config, aws, yes=args.yes)


def _apply(config: EnvConfig, aws: Aws, *, yes: bool) -> int:
    plan = build_plan(config, aws)
    print(render_text(plan))
    if not yes:
        print("\nDry run. Re-run with --yes to apply.")
        return 1 if plan.blocked else 0
    try:
        result = apply_plan(plan, config, aws)
    except ApplyRefused as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    _print_data_status(config.environments, aws)
    if result.failed:
        print("\nFailed:", file=sys.stderr)
        for failure in result.failed:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("\nApply complete.")
    return 0


def _print_data_status(environments: Sequence[Environment], aws: Aws) -> None:
    # Informational only: an empty fresh resource is expected and never fails the apply.
    try:
        statuses = [s for env in environments for s in environment_status(env, aws)]
    except Exception as exc:
        print(f"\nwarning: data status unavailable: {exc}", file=sys.stderr)
        return
    print(f"\n{render_status_text(statuses)}")


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=DEFAULT_ENVS_PATH)

    parser = argparse.ArgumentParser(prog="rc-infra", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", parents=[common], help="validate the config")
    apply = commands.add_parser("apply", parents=[common], help="apply the config to AWS")
    apply.add_argument("--yes", action="store_true", help="actually apply")
    status = commands.add_parser("status", parents=[common], help="report empty and non-empty durable resources")
    status.add_argument("--environment", help="only this environment")
    status.add_argument("--format", choices=["text", "json"], default="text")
    outputs = commands.add_parser("outputs", parents=[common], help="print the deployment contract from stack outputs")
    outputs.add_argument("--environment", help="only this environment")
    return parser


def _check_account(config: EnvConfig) -> str | None:
    account = aws_module.caller_account(config.region)
    if account != config.account_id:
        return f"Refusing to continue: credentials are for account {account}, but the config targets {config.account_id}."
    return None


if __name__ == "__main__":
    raise SystemExit(main())
