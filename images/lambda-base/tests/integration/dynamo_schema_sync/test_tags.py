"""Ownership tags written by rc-dynamo-sync --apply.

Environment teardown deletes only tables carrying these tags, so they must be
present on tables the tool creates and on existing declared tables it adopts.
"""
from __future__ import annotations

import os

from . import dynamo_helpers as dh
from . import schema_cases as sc
from .cli_helpers import output, run_schema_sync


def expected_tags(**extra: str) -> dict:
    return {
        "ManagedBy": "rc-dynamo-sync",
        "LifecycleOwner": "rc-dynamo-sync",
        "Environment": os.environ["RC_ENVIRONMENT"],
        **extra,
    }


def test_apply_tags_the_tables_it_creates(managed_table):
    name = managed_table(sc.create_simple_table)

    result = run_schema_sync(
        tables="create_simple_table",
        apply=True,
        extra_args=("--tag", "Repository=retribalize-core"),
    )

    assert result.returncode == 0, output(result)
    assert dh.list_tags(name) == expected_tags(Repository="retribalize-core")


def test_apply_adds_missing_tags_to_an_existing_table(managed_table):
    name = managed_table(sc.create_simple_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": None})

    # Missing tags are reported but are not schema drift: exit 0, nothing written.
    dry = run_schema_sync(tables="create_simple_table")
    assert dry.returncode == 0, output(dry)
    assert "Would tag" in dry.stdout, output(dry)
    assert dh.list_tags(name) == {}

    apply = run_schema_sync(tables="create_simple_table", apply=True)
    assert apply.returncode == 0, output(apply)
    assert dh.list_tags(name) == expected_tags()

    again = run_schema_sync(tables="create_simple_table", apply=True)
    assert again.returncode == 0, output(again)
    assert "Tagging" not in again.stdout, output(again)


def test_tags_survive_a_table_recreate(managed_table, dump_bucket):
    name = managed_table(sc.recreate_sk_removed_table)
    dh.create_live_table(name, {"partition_key": "pk", "sort_key": "old_sk"})
    dh.put_items(name, [{"pk": "a", "old_sk": "1"}])

    result = run_schema_sync(
        tables="recreate_sk_removed_table",
        apply=True,
        extra_env={"DYNAMO_ALLOW_TABLE_RECREATE": "true"},
    )

    assert result.returncode == 0, output(result)
    assert dh.describe_schema(name)["key_schema"] == {"partition_key": "pk", "sort_key": None}
    assert dh.list_tags(name) == expected_tags()
