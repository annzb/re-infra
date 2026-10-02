from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

import pytest

RUNTIME = Path("/opt/rc-runtime.json")


@pytest.mark.skipif(not RUNTIME.exists(), reason="only inside the built image")
def test_runtime_metadata_matches_the_installed_package() -> None:
    runtime = json.loads(RUNTIME.read_text())
    assert runtime["packages"] == {"rc-dynamo": importlib.metadata.version("rc-dynamo")}
    assert runtime["python"].startswith("3.11.")
