"""Subprocess wrappers driving the rc-dynamo-sync and rc-dynamo-report console scripts.

Tests run the tools the way a deployment does: through their CLIs. The schema module
comes from DYNAMO_SCHEMA_MODULE (set in conftest), so no --schema-module flag is needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

# .../tests/integration/dynamo_schema_sync/cli_helpers.py -> images/lambda-base/
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# The console scripts are installed next to the interpreter running the tests.
BIN = Path(sys.executable).parent
SYNC_SCRIPT = BIN / "rc-dynamo-sync"
REPORT_SCRIPT = BIN / "rc-dynamo-report"

# --apply writes ownership tags, and those need an environment name.
TEST_ENVIRONMENT = "itest"

# Sentinel so a caller can pass an explicit empty --dump-bucket (to exercise the
# "dump bucket required" guard) distinctly from "don't pass the flag at all".
_UNSET: Any = object()


def _command(script: Path, entry_point: str, args: Sequence[str]) -> list[str]:
    if script.exists():
        return [str(script), *args]
    # Fall back to the entry point when the console scripts are not on disk.
    code = (
        f"import sys; from rc_lambda_base.dynamo.cli import {entry_point}; "
        f"sys.exit({entry_point}())"
    )
    return [sys.executable, "-c", code, *args]


def _run(cmd: Sequence[str], extra_env: Mapping[str, str] | None) -> CompletedProcess[str]:
    env = {**os.environ, **(extra_env or {})}
    # The schema modules live under tests/, so the project root must be importable
    # inside the subprocess however pytest was launched.
    entries = [str(PROJECT_ROOT)]
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return subprocess.run(
        list(cmd),
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def run_schema_sync(
    *,
    tables: str,
    apply: bool = False,
    dump_bucket: Any = _UNSET,
    environment: str | None = TEST_ENVIRONMENT,
    extra_args: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> CompletedProcess[str]:
    args: list[str] = ["--tables", tables]
    if apply:
        args.append("--apply")
    if dump_bucket is not _UNSET:
        args += ["--dump-bucket", dump_bucket]
    if environment:
        args += ["--environment", environment]
    args += list(extra_args)
    return _run(_command(SYNC_SCRIPT, "sync_main", args), extra_env)


def run_schema_report(
    *,
    tables: str,
    fmt: str = "text",
    extra_args: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> CompletedProcess[str]:
    args = ["--tables", tables, "--format", fmt, *extra_args]
    return _run(_command(REPORT_SCRIPT, "report_main", args), extra_env)


def output(result: CompletedProcess[str]) -> str:
    """Render stdout+stderr for inclusion in assertion messages."""
    return f"\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
