"""Fixtures and safety guards for the schema-sync integration tests.

These tests intentionally create, delete, and recreate DynamoDB tables, so they
must only ever run against a local LocalStack endpoint. The guard below refuses
to run otherwise.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

# ── Establish a unique run id and env defaults BEFORE any test module imports
# schema_cases. conftest is imported ahead of the test modules during collection,
# so these module-level side effects run first and every table name (and every
# migration subprocess, which inherits os.environ) resolves consistently. ──
os.environ.setdefault("DYNAMO_SCHEMA_TEST_RUN_ID", uuid4().hex[:8])
os.environ.setdefault(
    "DYNAMO_SCHEMA_MODULE", "tests.integration.dynamo_schema_sync.schema_cases"
)
os.environ.setdefault("DYNAMO_SCHEMA_POLL_SECONDS", "1")
# Written to the Environment tag; --apply requires it.
os.environ.setdefault("RC_ENVIRONMENT", "schema-sync-test")
DUMP_BUCKET = os.environ.setdefault(
    "DYNAMO_SCHEMA_DUMP_BUCKET", "rc-local-schema-sync-test-dumps"
)


def _assert_safe_local_environment() -> None:
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    assert endpoint, "AWS_ENDPOINT_URL must be set to a LocalStack endpoint"
    assert "localstack" in endpoint or endpoint.startswith("http://localhost"), (
        f"Refusing to run against non-local endpoint: {endpoint!r}"
    )
    assert os.environ.get("AWS_ACCESS_KEY_ID") == "test", "AWS_ACCESS_KEY_ID must be 'test'"
    assert os.environ.get("AWS_SECRET_ACCESS_KEY") == "test", "AWS_SECRET_ACCESS_KEY must be 'test'"
    prefix = os.environ.get("DYNAMO_SCHEMA_TEST_PREFIX", "")
    assert "schema-sync-test" in prefix, (
        f"DYNAMO_SCHEMA_TEST_PREFIX must contain 'schema-sync-test', got {prefix!r}"
    )


# Fail collection immediately if the environment is unsafe.
_assert_safe_local_environment()


def table_prefix() -> str:
    return f"{os.environ['DYNAMO_SCHEMA_TEST_PREFIX']}-{os.environ['DYNAMO_SCHEMA_TEST_RUN_ID']}"


@pytest.fixture(autouse=True)
def _safety_guard():
    _assert_safe_local_environment()


@pytest.fixture(scope="session")
def dump_bucket() -> str:
    return DUMP_BUCKET


@pytest.fixture(scope="session", autouse=True)
def _schema_sync_session():
    from . import dynamo_helpers as dh

    prefix = table_prefix()
    # Pre-run cleanup: remove leftover tables from a prior interrupted run that
    # reused this run id (only relevant when the id is set manually).
    dh.delete_tables_by_prefix(prefix)
    dh.ensure_bucket(DUMP_BUCKET)

    yield

    # Post-session cleanup: delete every temporary table and dump object.
    dh.delete_tables_by_prefix(prefix)
    dh.delete_bucket_objects(DUMP_BUCKET, prefix="dynamo-schema-sync/")


@pytest.fixture
def managed_table():
    """Factory that guarantees a clean slate for a table and deletes it after.

    Pass a schema_cases table instance (or a raw table name). The named table is
    deleted up-front (so the test starts from a known state) and again in
    teardown.
    """
    from . import dynamo_helpers as dh

    names: list[str] = []

    def _manage(table_or_name) -> str:
        name = getattr(table_or_name, "table_name", table_or_name)
        dh.delete_table(name)
        names.append(name)
        return name

    yield _manage

    for name in names:
        try:
            dh.delete_table(name)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
