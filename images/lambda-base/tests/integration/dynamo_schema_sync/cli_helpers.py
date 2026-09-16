"""Subprocess wrappers driving the installed rc-dynamo-sync / rc-dynamo-report scripts.

Tests run the tools exactly the way a deploy would, via their console scripts.
The schema module is selected through ``DYNAMO_SCHEMA_MODULE`` and the
environment tag through ``RC_ENVIRONMENT`` (both defaulted by conftest), so no
flags are needed for either.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Mapping, Optional, Sequence

# .../images/lambda-base/tests/integration/dynamo_schema_sync/cli_helpers.py -> images/lambda-base/
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Sentinel so callers can pass an explicit empty --dump-bucket (to exercise the
# "dump bucket required" guard) distinctly from "don't pass the flag at all".
_UNSET: Any = object()


def console_script(name: str) -> str:
    """Path to an installed console script, preferring the running interpreter's environment."""
    candidate = Path(sys.executable).with_name(name)
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} is not installed; run `uv sync` in images/lambda-base")
    return found


def _run(cmd: Sequence[str], extra_env: Optional[Mapping[str, str]]) -> "CompletedProcess[str]":
    env = {**os.environ, **(extra_env or {})}
    # The project root makes the tests.integration schema modules importable.
    prior = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT_ROOT), *([prior] if prior else [])])

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
    extra_args: Sequence[str] = (),
    extra_env: Optional[Mapping[str, str]] = None,
) -> "CompletedProcess[str]":
    cmd = [console_script("rc-dynamo-sync"), "--tables", tables]
    if apply:
        cmd.append("--apply")
    if dump_bucket is not _UNSET:
        cmd += ["--dump-bucket", dump_bucket]
    cmd += list(extra_args)
    return _run(cmd, extra_env)


def run_schema_report(
    *,
    tables: str,
    fmt: str = "text",
    extra_args: Sequence[str] = (),
    extra_env: Optional[Mapping[str, str]] = None,
) -> "CompletedProcess[str]":
    cmd = [console_script("rc-dynamo-report"), "--tables", tables, "--format", fmt, *extra_args]
    return _run(cmd, extra_env)


def output(result: "CompletedProcess[str]") -> str:
    """Render stdout+stderr for inclusion in assertion messages."""
    return f"\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
