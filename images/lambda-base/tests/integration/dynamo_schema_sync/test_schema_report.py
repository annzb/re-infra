"""The drift report: rc-dynamo-report.

This is the tool for the "somebody changed a table in the console" problem, so
what matters is that it *finds* undeclared work and emits a declaration that can
actually be pasted into the model.
"""
from __future__ import annotations

import json

from . import dynamo_helpers as dh
from . import schema_cases as sc
from .cli_helpers import output
from .cli_helpers import run_schema_report as run_report


def test_clean_table_reports_nothing(managed_table):
    name = managed_table(sc.missing_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI-A": {"partition_key": "gsi_a", "sort_key": None}},
    )

    result = run_report(tables="missing_gsi_table")

    assert result.returncode == 0, output(result)
    assert "OK" in result.stdout, output(result)


def test_undeclared_index_and_sort_key_are_reported(managed_table):
    name = managed_table(sc.ignore_composite_remote_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={
            "GSI1-Test": {"partition_key": "gsi_pk", "sort_key": "gsi_sk"},
            "Console-Made": {"partition_key": "payload", "sort_key": None},
        },
        attribute_types={"gsi_pk": "S", "gsi_sk": "S"},
    )

    result = run_report(tables="ignore_composite_remote_gsi_table")

    # Exit 3: the deploy would leave all of this alone, but the whole point of
    # the report is to surface it.
    assert result.returncode == 3, output(result)
    assert "gsi_sk" in result.stdout, output(result)
    assert "Console-Made" in result.stdout, output(result)
    assert "Undeclared" in result.stdout, output(result)


def test_json_format_is_machine_readable(managed_table):
    name = managed_table(sc.ignore_composite_remote_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI1-Test": {"partition_key": "gsi_pk", "sort_key": "gsi_sk"}},
        attribute_types={"gsi_pk": "S", "gsi_sk": "S"},
    )

    result = run_report(tables="ignore_composite_remote_gsi_table", fmt="json")

    assert result.returncode == 3, output(result)
    payload = json.loads(result.stdout)
    kinds = [f["kind"] for entry in payload for f in entry["findings"]]
    assert "undeclared_sort_key" in kinds


def test_python_format_emits_a_pasteable_declaration(managed_table):
    # gsi_sk IS a declared field on Gsi1Item, so this is the clean case: the
    # emitted block should be complete and carry no blocker.
    name = managed_table(sc.ignore_composite_remote_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={
            "GSI1-Test": {
                "partition_key": "gsi_pk",
                "sort_key": "gsi_sk",
                "projection": "KEYS_ONLY",
            }
        },
        attribute_types={"gsi_pk": "S", "gsi_sk": "S"},
    )

    result = run_report(tables="ignore_composite_remote_gsi_table", fmt="python")

    assert result.returncode == 3, output(result)
    assert "gsi_schemas" in result.stdout, output(result)
    assert "'partition_key': 'gsi_pk'" in result.stdout, output(result)
    assert "'sort_key': 'gsi_sk'" in result.stdout, output(result)
    assert "'projection': 'KEYS_ONLY'" in result.stdout, output(result)
    assert "BLOCKER" not in result.stdout, output(result)


def test_python_format_flags_a_sort_key_that_is_not_a_model_field(managed_table):
    # The GSI2-EntityType/CreatedAt trap: naming a non-field in gsi_schemas makes
    # BaseItem raise at class-definition time, so importing the schema module fails
    # and nothing boots. The emitted block must refuse to be half-pasted.
    name = managed_table(sc.remote_only_gsi_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"Console-Made": {"partition_key": "NotAField", "sort_key": None}},
    )

    result = run_report(tables="remote_only_gsi_table", fmt="python")

    assert result.returncode == 3, output(result)
    assert "BLOCKER" in result.stdout, output(result)
    assert "NotAField: Optional[str] = None" in result.stdout, output(result)


def test_undeclared_attributes_are_found_when_sampling(managed_table):
    name = managed_table(sc.remote_only_gsi_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": None})
    dh.put_items(name, [{"pk": "a", "payload": "declared", "console_column": "surprise"}])

    result = run_report(tables="remote_only_gsi_table", extra_args=("--sample-items", "50"))

    assert result.returncode == 3, output(result)
    assert "console_column" in result.stdout, output(result)
    # Reported with a denominator, never as a bare list.
    assert "/1 sampled items" in result.stdout, output(result)
    assert "payload" not in result.stdout.split("console_column")[1], output(result)


def test_attribute_sampling_is_off_by_default(managed_table):
    name = managed_table(sc.remote_only_gsi_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": None})
    dh.put_items(name, [{"pk": "a", "console_column": "surprise"}])

    result = run_report(tables="remote_only_gsi_table")

    assert result.returncode == 0, output(result)
    assert "console_column" not in result.stdout, output(result)


def test_large_sample_requires_explicit_opt_in(managed_table):
    managed_table(sc.remote_only_gsi_table)

    result = run_report(tables="remote_only_gsi_table", extra_args=("--sample-items", "99999"))

    assert result.returncode != 0, output(result)
    assert "allow-large-sample" in (result.stdout + result.stderr), output(result)


def test_report_never_mutates(managed_table):
    name = managed_table(sc.changed_gsi_pk_table)
    dh.create_live_table(
        name,
        {"partition_key": "pk", "sort_key": None},
        gsis={"GSI1-Test": {"partition_key": "old_gsi_pk", "sort_key": None}},
    )
    before = dh.describe_schema(name)

    result = run_report(tables="changed_gsi_pk_table")

    assert result.returncode == 3, output(result)
    assert "rebuild_gsi" in result.stdout, output(result)
    assert dh.describe_schema(name) == before
