"""rc-dynamo-report rendering of table-option and unsupported-feature findings."""

import json
from types import SimpleNamespace
from typing import ClassVar

from rc_dynamo import BaseItem, BaseTable, TableOptions
from rc_dynamo.schema.report import build_report, python_suggestions, render_text
from rc_dynamo.utils.settings import Settings

from ..fakes import FakeDynamoClient, live_description


class _Session(BaseItem):
    partition_key: ClassVar[str] = "pk"
    pk: str


def _table(client, **options) -> BaseTable:
    class _Sessions(BaseTable[_Session]):
        table_name = "rc-sessions"
        item_model = _Session
        table_options = TableOptions(**options)

    return _Sessions(SimpleNamespace(meta=SimpleNamespace(client=client)))


LSI = [
    {
        "IndexName": "by-created",
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "created", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }
]


def _report(table, fmt="text"):
    return build_report([table], fmt=fmt, settings=Settings())


def test_report_header_names_the_new_permission_and_what_is_not_inspected():
    output, _ = _report(_table(FakeDynamoClient(live_description())))
    assert "DYNAMO_ALLOW_PROTECTION_DOWNGRADE=false" in output
    assert "Not inspected:" in output
    assert "Not compared: TTL" not in output


def test_matching_options_report_ok():
    client = FakeDynamoClient(live_description(), ttl_attribute="expiresAt")
    output, any_findings = _report(_table(client, ttl_attribute="expiresAt"))
    assert any_findings is False
    assert "rc-sessions: OK" in output


def test_report_groups_in_place_changes_unsupported_and_undeclared():
    client = FakeDynamoClient(live_description(LocalSecondaryIndexes=LSI), pitr=True)
    table = _table(client, ttl_attribute="expiresAt", deletion_protection=True)

    output, any_findings = _report(table)

    assert any_findings is True
    in_place = output.split("Will change on next deploy (in place):")[1].split("  Unsupported")[0]
    assert "[update_options]" in in_place and "TTL" in in_place
    assert "deletion protection" in in_place
    unsupported = output.split("Unsupported live features")[1].split("Undeclared")[0]
    assert "by-created" in unsupported
    undeclared = output.split("Undeclared (left alone):")[1]
    assert "point-in-time recovery is enabled" in undeclared


def test_report_marks_protection_downgrades_with_their_permission():
    client = FakeDynamoClient(live_description(), pitr=True)
    output, _ = _report(_table(client, point_in_time_recovery=False))
    assert "[disable_protection]" in output
    assert "(requires DYNAMO_ALLOW_PROTECTION_DOWNGRADE=true)" in output


def test_report_separates_a_recreate_and_its_refusal_from_in_place_changes():
    client = FakeDynamoClient(
        live_description(partition_key="old_pk", LocalSecondaryIndexes=LSI), pitr=False
    )
    output, _ = _report(_table(client, point_in_time_recovery=True))

    cannot = output.split("Cannot be applied")[1].split("  Needs a new table")[0]
    assert "[none]" in cannot and "refused" in cannot and "local secondary index" in cannot
    new_table = output.split("Needs a new table on next deploy:")[1].split("  Will change")[0]
    assert "[recreate_table]" in new_table
    assert "(requires DYNAMO_ALLOW_TABLE_RECREATE=true)" in new_table
    in_place = output.split("Will change on next deploy (in place):")[1]
    assert "point-in-time recovery" in in_place.split("Unsupported")[0]


def test_json_report_carries_the_new_kinds():
    client = FakeDynamoClient(
        live_description(StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"})
    )
    output, _ = _report(_table(client, ttl_attribute="expiresAt"), fmt="json")
    findings = json.loads(output)[0]["findings"]
    assert {(f["kind"], f.get("attribute")) for f in findings} == {
        ("conflicting_table_option", "ttl_attribute"),
        ("unsupported_live_feature", "stream_view_type"),
    }


def test_render_text_without_findings_is_ok():
    assert render_text("tbl", []) == "tbl: OK -- live schema matches the declaration"


def test_python_format_adopts_live_options_merged_with_declared_ones():
    client = FakeDynamoClient(
        live_description(
            billing_mode="PROVISIONED",
            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
        ),
        ttl_attribute="expiresAt",
    )
    table = _table(client, deletion_protection=False)
    findings = table.schema_diff().findings

    block = python_suggestions(table, findings)

    assert block is not None
    assert "gsi_schemas" not in block
    assert "# on _Sessions:" in block
    assert "    table_options = TableOptions(\n" in block
    assert "        billing_mode='PROVISIONED',\n" in block
    assert "        deletion_protection=False,\n" in block
    assert "        ttl_attribute='expiresAt',\n" in block
    assert "        stream_view_type='NEW_IMAGE',\n" in block


def test_python_format_has_nothing_to_adopt_for_a_plain_table():
    output, _ = _report(_table(FakeDynamoClient(live_description())), fmt="python")
    assert "Nothing to adopt" in output
