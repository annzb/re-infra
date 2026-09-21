"""rc-infra: validate the environment config and apply it.

    rc-infra validate           config only, no AWS access
    rc-infra apply [--yes]      show what AWS is missing, then carry it out with --yes

Exit codes: 0 success, 1 invalid config / blocked plan / failed apply, 2 usage error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from rc_infra import aws as aws_module
from rc_infra.apply import ApplyRefused, apply_plan
from rc_infra.env_config import DEFAULT_ENVS_PATH, EnvConfig, EnvConfigError, load_env_config
from rc_infra.planner import build_plan, render_text


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

    account_error = _check_account(config)
    if account_error:
        print(account_error, file=sys.stderr)
        return 1

    aws = aws_module.connect(config.region)
    plan = build_plan(config, aws)

    print(render_text(plan))
    if not args.yes:
        print("\nDry run. Re-run with --yes to apply.")
        return 1 if plan.blocked else 0
    try:
        result = apply_plan(plan, config, aws)
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
    common.add_argument("--config", type=Path, default=DEFAULT_ENVS_PATH)

    parser = argparse.ArgumentParser(prog="rc-infra", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", parents=[common], help="validate the config")
    apply = commands.add_parser("apply", parents=[common], help="apply the config to AWS")
    apply.add_argument("--yes", action="store_true", help="actually apply")
    return parser


def _check_account(config: EnvConfig) -> str | None:
    account = aws_module.caller_account(config.region)
    if account != config.account_id:
        return f"Refusing to continue: credentials are for account {account}, but the config targets {config.account_id}."
    return None


if __name__ == "__main__":
    raise SystemExit(main())
