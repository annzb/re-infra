"""Ownership tags.

Environment teardown deletes a DynamoDB table only when it carries these tags, so
rc-dynamo-sync must write them on every table it manages -- including tables that
already existed before the tool started managing them.
"""

from __future__ import annotations

from . import dynamo_helpers as dh
from . import schema_cases as sc
from .cli_helpers import TEST_ENVIRONMENT, output, run_schema_sync

OWNERSHIP = {
    "ManagedBy": "rc-dynamo-sync",
    "LifecycleOwner": "rc-dynamo-sync",
    "Environment": TEST_ENVIRONMENT,
}


def test_created_table_carries_ownership_tags(managed_table):
    name = managed_table(sc.create_simple_table)

    result = run_schema_sync(tables="create_simple_table", apply=True)

    assert result.returncode == 0, output(result)
    assert OWNERSHIP.items() <= dh.table_tags(name).items()


def test_existing_table_is_tagged_on_the_next_apply(managed_table):
    name = managed_table(sc.create_simple_table)
    assert run_schema_sync(tables="create_simple_table", apply=True).returncode == 0
    # An untagged table is what every table created before this tool looks like.
    dh.untag_table(name, list(OWNERSHIP))
    assert not OWNERSHIP.items() <= dh.table_tags(name).items()

    result = run_schema_sync(tables="create_simple_table", apply=True)

    assert result.returncode == 0, output(result)
    assert OWNERSHIP.items() <= dh.table_tags(name).items()


def test_extra_tags_are_written_alongside_the_ownership_tags(managed_table):
    name = managed_table(sc.create_simple_table)

    result = run_schema_sync(
        tables="create_simple_table",
        apply=True,
        extra_args=("--tag", "Repository=retribalize-core"),
    )

    assert result.returncode == 0, output(result)
    tags = dh.table_tags(name)
    assert OWNERSHIP.items() <= tags.items()
    assert tags["Repository"] == "retribalize-core"


def test_tags_the_tool_does_not_own_are_left_alone(managed_table):
    name = managed_table(sc.create_simple_table)
    assert run_schema_sync(tables="create_simple_table", apply=True).returncode == 0
    dh.tag_table(name, {"CostCentre": "research"})

    result = run_schema_sync(tables="create_simple_table", apply=True)

    assert result.returncode == 0, output(result)
    assert dh.table_tags(name)["CostCentre"] == "research"


def test_dry_run_does_not_write_tags(managed_table):
    name = managed_table(sc.create_simple_table)
    assert run_schema_sync(tables="create_simple_table", apply=True).returncode == 0
    dh.untag_table(name, list(OWNERSHIP))

    result = run_schema_sync(tables="create_simple_table")

    assert result.returncode == 0, output(result)
    assert not OWNERSHIP.items() <= dh.table_tags(name).items()
