#!/usr/bin/env bash
# The full quality gate, in one place. compose-tests.yaml runs this inside the test
# container, and CI runs compose - so what CI checks is what you can run locally:
#
#   docker build -t rc-local/rc-dynamo:dev .
#   docker compose -f compose-tests.yaml run --rm --build tests
#
# Run it directly from the package root with LocalStack already up:
#   uv run --project tests bash tests/run-checks.sh
set -euo pipefail

echo "==> lockfiles"
uv lock --check
uv lock --check --project tests

echo "==> ruff"
ruff check src tests
ruff format --check src tests

echo "==> mypy"
mypy

echo "==> unit tests"
pytest tests/unit

echo "==> integration tests (LocalStack)"
pytest tests/integration
