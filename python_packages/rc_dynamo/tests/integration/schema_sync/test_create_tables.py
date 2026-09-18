"""Table-creation behaviors of rc-dynamo-sync."""

from __future__ import annotations

from . import dynamo_helpers as dh
from . import schema_cases as sc
from .cli_helpers import output, run_schema_sync


def test_dry_run_missing_table_reports_diff_without_creating(managed_table):
    name = managed_table(sc.create_simple_table)

    result = run_schema_sync(tables="create_simple_table")

    assert result.returncode == 3, output(result)
    assert "Missing table" in result.stdout, output(result)
    assert not dh.table_exists(name)


def test_apply_creates_simple_table(managed_table):
    name = managed_table(sc.create_simple_table)

    result = run_schema_sync(tables="create_simple_table", apply=True)

    assert result.returncode == 0, output(result)
    assert dh.table_exists(name)

    schema = dh.describe_schema(name)
    assert schema["key_schema"] == {"partition_key": "pk", "sort_key": None}
    assert schema["gsis"] == {}

    described = dh.dynamo_client().describe_table(TableName=name)["Table"]
    assert described.get("BillingModeSummary", {}).get("BillingMode") == "PAY_PER_REQUEST"

    second = run_schema_sync(tables="create_simple_table")
    assert second.returncode == 0, output(second)
    assert "OK" in second.stdout, output(second)


def test_apply_creates_composite_primary_key_table(managed_table):
    name = managed_table(sc.create_composite_table)

    result = run_schema_sync(tables="create_composite_table", apply=True)

    assert result.returncode == 0, output(result)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": "sk"}

    described = dh.dynamo_client().describe_table(TableName=name)["Table"]
    attr_names = {a["AttributeName"] for a in described["AttributeDefinitions"]}
    assert {"pk", "sk"} <= attr_names

    assert run_schema_sync(tables="create_composite_table").returncode == 0


def test_apply_creates_table_with_declared_gsis(managed_table):
    name = managed_table(sc.create_gsi_table)

    result = run_schema_sync(tables="create_gsi_table", apply=True)

    assert result.returncode == 0, output(result)
    schema = dh.describe_schema(name)
    assert set(schema["gsis"]) == {"GSI-A", "GSI-B"}
    assert schema["gsis"]["GSI-A"] == {"partition_key": "gsi_a", "sort_key": None}
    assert schema["gsis"]["GSI-B"] == {"partition_key": "gsi_b", "sort_key": None}

    described = dh.dynamo_client().describe_table(TableName=name)["Table"]
    for gsi in described.get("GlobalSecondaryIndexes", []):
        assert gsi["IndexStatus"] == "ACTIVE", gsi

    assert run_schema_sync(tables="create_gsi_table").returncode == 0
