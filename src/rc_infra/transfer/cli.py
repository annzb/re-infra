"""rc-data-transfer: copy data between DynamoDB tables or S3 buckets, by hand only.

    rc-data-transfer table  --source <table ARN>  --target <table ARN>  (--dry-run | --apply)
    rc-data-transfer bucket --source <bucket ARN> --target <bucket ARN> (--dry-run | --apply) [--include-versions]
    rc-data-transfer resolve-copy --source-layout legacy|current --source-env <env> --source-resource <bucket>
                                  --target-env <env> --target-resource <bucket> (--dry-run | --apply)

Never run by rc-infra or by any workflow. Always dry-run first. See docs/DATA_TRANSFER.md.

Exit codes: 0 checked or copied, 1 refused or failed, 2 usage error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from rc_infra import aws
from rc_infra.env_config import BUCKET_LOGICAL_IDS, DEFAULT_ENVS_PATH, EnvConfigError, load_env_config
from rc_infra.transfer import legacy, resolve_current
from rc_infra.transfer.arns import ArnError, BucketArn, TableArn, parse_bucket_arn, parse_table_arn
from rc_infra.transfer.jobs import BotoClients, Clients, transfer_bucket, transfer_table
from rc_infra.transfer.models import Outcome, Report

_PURPOSES = {**{purpose: purpose for purpose in BUCKET_LOGICAL_IDS}, **{logical_id: purpose for purpose, logical_id in BUCKET_LOGICAL_IDS.items()}}


def main(argv: Sequence[str] | None = None, clients: Clients | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    clients = clients or BotoClients()

    if args.command == "table":
        report = transfer_table(clients, args.source, args.target, apply=args.apply)
    elif args.command == "bucket":
        report = transfer_bucket(clients, args.source, args.target, apply=args.apply, include_versions=args.include_versions)
    else:
        resolved = _resolve(args, clients, parser.error)
        if resolved is None:
            return 1
        source, target = resolved
        print(f"source: {source.arn}\ntarget: {target.arn}")
        print("equivalent: " + shlex.join(_bucket_command(source, target, args)) + "\n")
        report = transfer_bucket(clients, source, target, apply=args.apply, include_versions=args.include_versions)

    print(report.to_json() if args.format == "json" else report.to_text())
    return _exit_code(report)


def _resolve(args: argparse.Namespace, clients: Clients, usage_error: Callable[[str], None]) -> tuple[BucketArn, BucketArn] | None:
    for flag in ("source_resource", "target_resource"):
        if getattr(args, flag) not in _PURPOSES:
            usage_error(
                f"--{flag.replace('_', '-')}: {getattr(args, flag)!r} is not a bucket; one of {', '.join(BUCKET_LOGICAL_IDS.values())}. "
                "Tables cannot be resolved by name until retribalize-core publishes its table contract; pass ARNs to `table` instead."
            )
    try:
        config = load_env_config(args.config)
    except EnvConfigError as exc:
        print(f"{args.config} is invalid: {exc}", file=sys.stderr)
        return None
    stacks = aws.connect(config.region).stacks
    try:
        if args.source_layout == "legacy":
            source = legacy.bucket(args.source_env, _PURPOSES[args.source_resource], clients.account())
        else:
            source = resolve_current.bucket(config, stacks, args.source_env, _PURPOSES[args.source_resource])
        target = resolve_current.bucket(config, stacks, args.target_env, _PURPOSES[args.target_resource])
    except (ValueError, resolve_current.Unresolved) as exc:
        print(f"cannot resolve: {exc}", file=sys.stderr)
        return None
    return parse_bucket_arn(source), parse_bucket_arn(target)


def _bucket_command(source: BucketArn, target: BucketArn, args: argparse.Namespace) -> list[str]:
    command = ["rc-data-transfer", "bucket", "--source", source.arn, "--target", target.arn, "--apply" if args.apply else "--dry-run"]
    return [*command, "--include-versions"] if args.include_versions else command


def _exit_code(report: Report) -> int:
    if report.outcome is Outcome.INTERRUPTED:
        return 130
    return 0 if report.ok else 1


def _arn(parse: Callable[[str], TableArn | BucketArn]) -> Callable[[str], TableArn | BucketArn]:
    def convert(value: str) -> TableArn | BucketArn:
        try:
            return parse(value)
        except ArnError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return convert


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    mode = common.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_false", dest="apply", help="check everything, write nothing")
    mode.add_argument("--apply", action="store_true", dest="apply", help="copy the data")
    common.add_argument("--format", choices=["text", "json"], default="text")

    parser = argparse.ArgumentParser(prog="rc-data-transfer", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    table = commands.add_parser("table", parents=[common], help="copy every item into an empty table with identical configuration")
    table.add_argument("--source", required=True, type=_arn(parse_table_arn))
    table.add_argument("--target", required=True, type=_arn(parse_table_arn))

    for name, help_text in (
        ("bucket", "copy every object into an empty bucket"),
        ("resolve-copy", "resolve two buckets by environment and logical name, then copy"),
    ):
        command = commands.add_parser(name, parents=[common], help=help_text)
        command.add_argument("--include-versions", action="store_true", help="copy every object version, oldest first")
        if name == "bucket":
            command.add_argument("--source", required=True, type=_arn(parse_bucket_arn))
            command.add_argument("--target", required=True, type=_arn(parse_bucket_arn))
        else:
            command.add_argument("--source-layout", required=True, choices=["legacy", "current"])
            command.add_argument("--source-env", required=True)
            command.add_argument("--source-resource", required=True)
            command.add_argument("--target-env", required=True)
            command.add_argument("--target-resource", required=True)
            command.add_argument("--config", type=Path, default=DEFAULT_ENVS_PATH)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
