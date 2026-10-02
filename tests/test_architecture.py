"""Structural rules that keep one-time migration knowledge out of the normal deployment path."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.conftest import REPO_ROOT

PACKAGE = REPO_ROOT / "src" / "rc_infra"
TRANSFER = PACKAGE / "transfer"


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_deployment_code_never_imports_the_transfer_package() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT)): sorted(m for m in _imports(path) if m.startswith("rc_infra.transfer"))
        for path in PACKAGE.rglob("*.py")
        if TRANSFER not in path.parents
    }
    assert {path: modules for path, modules in offenders.items() if modules} == {}


def test_only_the_resolver_and_cli_know_the_legacy_layout() -> None:
    allowed = {TRANSFER / "cli.py", TRANSFER / "legacy.py"}
    users = {path for path in PACKAGE.rglob("*.py") if path not in allowed and "rc_infra.transfer.legacy" in _imports(path)}
    assert users == set()


def test_no_workflow_runs_a_data_transfer() -> None:
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    assert [str(w.relative_to(REPO_ROOT)) for w in workflows if "rc-data-transfer" in w.read_text() or "rc_infra.transfer" in w.read_text()] == []
