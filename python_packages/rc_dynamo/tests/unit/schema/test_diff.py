"""Declared-vs-live schema comparison.

The governing rule under test: `None` in a declaration means "not modeled --
do not enforce, inherit what is live", and such gaps surface as UNDECLARED
findings whose only remedy is a human editing the Python.
"""

import pytest

from rc_dynamo.schema.diff import (
    FindingKind,
    Permission,
    Remedy,
    Severity,
    diff_schemas,
    normalize_non_key_attributes,
    resolve_projection,
)
from rc_dynamo.schema.options import UNMANAGED, TableOptions

PK_ONLY = {"partition_key": "pk", "sort_key": None}


def gsi(partition_key="a", sort_key=None, projection=None, non_key_attributes=None):
    return {
        "partition_key": partition_key,
        "sort_key": sort_key,
        "projection": projection,
        "non_key_attributes": non_key_attributes,
    }


def schema(key_schema=None, gsis=None, attribute_types=None, options=None, features=None):
    built = {
        "table_name": "tbl",
        "key_schema": key_schema or dict(PK_ONLY),
        "gsis": gsis or {},
        "attribute_types": attribute_types or {},
    }
    if options is not None:
        built["options"] = options
    if features is not None:
        built["features"] = features
    return built


def declared(**options):
    """An expected_schema() dict declaring these TableOptions."""
    return schema(options=TableOptions(**options).as_schema())


def live(features=None, **options):
    """An actual_schema() dict: a plain on-demand table, overridden by `options`."""
    return schema(
        options={
            "billing_mode": "PAY_PER_REQUEST",
            "table_class": "STANDARD",
            "deletion_protection": False,
            "point_in_time_recovery": False,
            "ttl_attribute": None,
            "stream_view_type": None,
            **options,
        },
        features=features or {},
    )


def kinds(diff):
    return [f.kind for f in diff.findings]


def only(diff):
    assert len(diff.findings) == 1, [f.message for f in diff.findings]
    return diff.findings[0]


ALL_GRANTED = {Permission.PRUNE_UNDECLARED: True, Permission.ALLOW_TABLE_RECREATE: True}
NONE_GRANTED: dict = {}


# ────────────────────────────── clean / missing ──────────────────────────────


def test_identical_schemas_are_clean():
    both = schema(gsis={"GSI1": gsi("a", "b", "ALL")})
    assert diff_schemas(both, both).is_clean


def test_absent_table_is_a_single_missing_finding():
    finding = only(diff_schemas(schema(), None))
    assert finding.kind is FindingKind.MISSING_TABLE
    assert finding.remedy is Remedy.CREATE_TABLE
    assert finding.required_permission is None


def test_declared_gsi_absent_live_is_created_without_permission():
    diff = diff_schemas(schema(gsis={"GSI1": gsi()}), schema())
    finding = only(diff)
    assert finding.kind is FindingKind.MISSING_GSI
    assert finding.remedy is Remedy.CREATE_GSI
    assert finding.required_permission is None
    assert diff.actionable(NONE_GRANTED) == (finding,)


# ─────────────────────────────── undeclared ───────────────────────────────


def test_undeclared_gsi_is_reported_and_needs_pruning_permission():
    diff = diff_schemas(schema(), schema(gsis={"Console": gsi("x")}))
    finding = only(diff)
    assert finding.kind is FindingKind.UNDECLARED_GSI
    assert finding.severity is Severity.UNDECLARED
    assert finding.remedy is Remedy.DELETE_GSI
    assert finding.required_permission is Permission.PRUNE_UNDECLARED

    # Ignored by default -- the point of the whole design. Crucially it does not
    # *block*: an index somebody created in the console must not fail a deploy.
    assert diff.actionable(NONE_GRANTED) == ()
    assert diff.blocked(NONE_GRANTED) == ()
    assert diff.requires_action(NONE_GRANTED) is False

    assert diff.actionable(ALL_GRANTED) == (finding,)
    assert diff.requires_action(ALL_GRANTED) is True


def test_unmodeled_sort_key_is_adopted_never_touched():
    # The gsis shorthand cannot express a composite index, so sort_key=None
    # must not be read as "the live index must have no sort key".
    diff = diff_schemas(
        schema(gsis={"GSI1": gsi("a", None)}),
        schema(gsis={"GSI1": gsi("a", "live_sk")}),
    )
    finding = only(diff)
    assert finding.kind is FindingKind.UNDECLARED_SORT_KEY
    assert finding.remedy is Remedy.ADOPT_DECLARATION
    assert finding.attribute == "live_sk"
    # Never automatable, so it neither runs nor blocks, under any permissions.
    assert diff.actionable(ALL_GRANTED) == ()
    assert diff.blocked(ALL_GRANTED) == ()
    assert diff.requires_action(ALL_GRANTED) is False


def test_unmodeled_non_default_projection_is_reported():
    diff = diff_schemas(
        schema(gsis={"GSI1": gsi("a", projection=None)}),
        schema(gsis={"GSI1": gsi("a", projection="KEYS_ONLY")}),
    )
    finding = only(diff)
    assert finding.kind is FindingKind.UNDECLARED_PROJECTION
    assert finding.remedy is Remedy.ADOPT_DECLARATION
    assert diff.actionable(ALL_GRANTED) == ()


def test_unmodeled_all_projection_is_not_reported():
    # ALL is the create-time default; there is nothing interesting to adopt.
    diff = diff_schemas(
        schema(gsis={"GSI1": gsi("a", projection=None)}),
        schema(gsis={"GSI1": gsi("a", projection="ALL")}),
    )
    assert diff.is_clean


def test_live_user_table_shape_produces_no_conflicts():
    # Regression for the real rc-*-users estate: three composite indexes and a
    # KEYS_ONLY projection, all declared HASH-only via the shorthand. This must
    # stay report-only or every deploy breaks.
    declared = schema(
        gsis={
            "GSI1-Email": gsi("email"),
            "GSI2-EntityType": gsi("EntityType"),
            "GSI3-NameToken": gsi("nameTokenPartition"),
            "GSI4-ReferralCommission": gsi("commissionReferrerId"),
        }
    )
    live = schema(
        gsis={
            "GSI1-Email": gsi("email", None, "ALL"),
            "GSI2-EntityType": gsi("EntityType", "CreatedAt", "ALL"),
            "GSI3-NameToken": gsi("nameTokenPartition", "nameToken", "KEYS_ONLY"),
            "GSI4-ReferralCommission": gsi(
                "commissionReferrerId", "commissionStatusMaturity", "ALL"
            ),
        }
    )

    diff = diff_schemas(declared, live)

    assert diff.of_severity(Severity.CONFLICT) == ()
    assert diff.actionable(NONE_GRANTED) == ()
    assert diff.blocked(NONE_GRANTED) == ()
    assert kinds(diff) == [
        FindingKind.UNDECLARED_SORT_KEY,  # GSI2
        FindingKind.UNDECLARED_SORT_KEY,  # GSI3
        FindingKind.UNDECLARED_PROJECTION,  # GSI3 KEYS_ONLY
        FindingKind.UNDECLARED_SORT_KEY,  # GSI4
    ]
    # Reported, but the deploy has nothing to do -- so it must exit clean.
    assert diff.requires_action(NONE_GRANTED) is False
    assert diff.requires_action(ALL_GRANTED) is False


# ─────────────────────────────── conflicts ───────────────────────────────


def test_gsi_partition_key_conflict_rebuilds_without_permission():
    diff = diff_schemas(
        schema(gsis={"GSI1": gsi("new")}),
        schema(gsis={"GSI1": gsi("old")}),
    )
    finding = only(diff)
    assert finding.kind is FindingKind.CONFLICTING_GSI_KEY
    assert finding.remedy is Remedy.REBUILD_GSI
    # Declaration-first: reconciling a declared index is ungated.
    assert finding.required_permission is None
    assert diff.actionable(NONE_GRANTED) == (finding,)
    assert diff.blocked(NONE_GRANTED) == ()


def test_declared_sort_key_conflict_rebuilds():
    diff = diff_schemas(
        schema(gsis={"GSI1": gsi("a", "want")}),
        schema(gsis={"GSI1": gsi("a", "have")}),
    )
    finding = only(diff)
    assert finding.kind is FindingKind.CONFLICTING_GSI_KEY
    assert finding.remedy is Remedy.REBUILD_GSI


def test_partition_key_conflict_suppresses_sort_key_noise():
    # One rebuild fixes both; reporting two findings for one index is noise.
    diff = diff_schemas(
        schema(gsis={"GSI1": gsi("new", "want")}),
        schema(gsis={"GSI1": gsi("old", "have")}),
    )
    assert kinds(diff) == [FindingKind.CONFLICTING_GSI_KEY]


def test_declared_projection_conflict_rebuilds():
    diff = diff_schemas(
        schema(gsis={"GSI1": gsi("a", projection="KEYS_ONLY")}),
        schema(gsis={"GSI1": gsi("a", projection="ALL")}),
    )
    finding = only(diff)
    assert finding.kind is FindingKind.CONFLICTING_PROJECTION
    assert finding.remedy is Remedy.REBUILD_GSI


def test_include_non_key_attributes_compared_as_a_set():
    # describe_table returns NonKeyAttributes unordered.
    declared = schema(gsis={"GSI1": gsi("a", projection="INCLUDE", non_key_attributes=["x", "y"])})
    live = schema(gsis={"GSI1": gsi("a", projection="INCLUDE", non_key_attributes=["y", "x"])})
    assert diff_schemas(declared, live).is_clean


def test_include_non_key_attribute_difference_is_a_conflict():
    declared = schema(gsis={"GSI1": gsi("a", projection="INCLUDE", non_key_attributes=["x", "y"])})
    live = schema(gsis={"GSI1": gsi("a", projection="INCLUDE", non_key_attributes=["x"])})
    assert only(diff_schemas(declared, live)).kind is FindingKind.CONFLICTING_PROJECTION


def test_primary_key_conflict_requires_table_recreate():
    diff = diff_schemas(
        schema(key_schema={"partition_key": "new_pk", "sort_key": None}),
        schema(),
    )
    finding = only(diff)
    assert finding.kind is FindingKind.CONFLICTING_TABLE_KEY
    assert finding.remedy is Remedy.RECREATE_TABLE
    assert finding.required_permission is Permission.ALLOW_TABLE_RECREATE
    assert diff.blocked(NONE_GRANTED) == (finding,)
    assert diff.actionable(ALL_GRANTED) == (finding,)


def test_key_attribute_type_conflict_requires_table_recreate():
    diff = diff_schemas(
        schema(attribute_types={"pk": "S"}),
        schema(attribute_types={"pk": "N"}),
    )
    finding = only(diff)
    assert finding.kind is FindingKind.CONFLICTING_ATTRIBUTE_TYPE
    assert finding.remedy is Remedy.RECREATE_TABLE


def test_undeclared_provisioned_billing_is_unsupported_not_drift():
    # Replaces the old UNMANAGED_BILLING_MODE observation: a recreate would build
    # the table on-demand, so it is a recreate blocker, but never acted on.
    diff = diff_schemas(declared(), live(billing_mode="PROVISIONED"))
    finding = only(diff)
    assert finding.kind is FindingKind.UNSUPPORTED_LIVE_FEATURE
    assert finding.attribute == "billing_mode"
    assert diff.actionable(ALL_GRANTED) == ()
    assert diff.blocked(ALL_GRANTED) == ()
    assert diff.requires_action(ALL_GRANTED) is False


# ────────────────────────── resolve_projection ──────────────────────────


def test_resolve_projection_prefers_the_declaration():
    resolved = resolve_projection(gsi("a", projection="KEYS_ONLY"), gsi("a", projection="ALL"))
    assert resolved == {"ProjectionType": "KEYS_ONLY"}


def test_resolve_projection_inherits_live_when_unmodeled():
    # The KEYS_ONLY -> ALL hazard: an unmodeled projection must never widen.
    resolved = resolve_projection(gsi("a", projection=None), gsi("a", projection="KEYS_ONLY"))
    assert resolved == {"ProjectionType": "KEYS_ONLY"}


def test_resolve_projection_inherits_live_include_attributes():
    resolved = resolve_projection(
        gsi("a", projection=None),
        gsi("a", projection="INCLUDE", non_key_attributes=["b", "a"]),
    )
    assert resolved == {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["a", "b"]}


def test_resolve_projection_defaults_to_all_for_a_brand_new_index():
    assert resolve_projection(gsi("a", projection=None), None) == {"ProjectionType": "ALL"}


@pytest.mark.parametrize(
    "value,expected", [(None, None), ([], frozenset()), (["b", "a"], frozenset({"a", "b"}))]
)
def test_normalize_non_key_attributes(value, expected):
    assert normalize_non_key_attributes(value) == expected


# ─────────────────────────────── table options ───────────────────────────────


def test_schema_dicts_without_options_compare_no_options():
    # Hand-built dicts (and old callers) carry no "options" / "features" at all.
    assert diff_schemas(schema(), schema()).is_clean


def test_unmanaged_options_are_never_turned_off():
    # Everything enabled live, nothing declared: reported for adoption, never changed.
    diff = diff_schemas(
        declared(),
        live(deletion_protection=True, point_in_time_recovery=True, ttl_attribute="expiresAt"),
    )
    assert {f.kind for f in diff.findings} == {FindingKind.UNDECLARED_TABLE_OPTION}
    assert {f.attribute for f in diff.findings} == {
        "deletion_protection",
        "point_in_time_recovery",
        "ttl_attribute",
    }
    assert all(f.remedy is Remedy.ADOPT_DECLARATION for f in diff.findings)
    assert diff.actionable(ALL_GRANTED) == ()
    assert diff.unfixable() == ()
    assert diff.requires_action(ALL_GRANTED) is False


def test_unmanaged_options_at_their_defaults_are_not_reported():
    assert diff_schemas(declared(), live()).is_clean


def test_matching_declared_options_are_clean():
    options = {
        "billing_mode": "PAY_PER_REQUEST",
        "table_class": "STANDARD",
        "deletion_protection": True,
        "point_in_time_recovery": True,
        "ttl_attribute": "expiresAt",
        "stream_view_type": "NEW_IMAGE",
    }
    assert diff_schemas(declared(**options), live(**options)).is_clean


@pytest.mark.parametrize(
    "option,declared_value,live_value",
    [
        ("ttl_attribute", "expiresAt", None),
        ("ttl_attribute", "expiresAt", "ttl"),
        ("ttl_attribute", None, "expiresAt"),
        ("point_in_time_recovery", True, False),
        ("deletion_protection", True, False),
        ("billing_mode", "PAY_PER_REQUEST", "PROVISIONED"),
        ("table_class", "STANDARD", "STANDARD_INFREQUENT_ACCESS"),
        ("table_class", "STANDARD_INFREQUENT_ACCESS", "STANDARD"),
        ("stream_view_type", "NEW_IMAGE", None),
        ("stream_view_type", "KEYS_ONLY", "NEW_IMAGE"),
        ("stream_view_type", None, "NEW_IMAGE"),
    ],
)
def test_declared_option_drift_is_updated_in_place_without_permission(
    option, declared_value, live_value
):
    diff = diff_schemas(declared(**{option: declared_value}), live(**{option: live_value}))
    finding = only(diff)
    assert finding.kind is FindingKind.CONFLICTING_TABLE_OPTION
    assert finding.severity is Severity.CONFLICT
    assert finding.remedy is Remedy.UPDATE_OPTIONS
    assert finding.attribute == option
    assert (finding.declared, finding.live) == (declared_value, live_value)
    assert finding.required_permission is None
    assert finding.needs_new_table is False
    assert diff.actionable(NONE_GRANTED) == (finding,)
    assert finding.to_json()["attribute"] == option


@pytest.mark.parametrize("option", ["point_in_time_recovery", "deletion_protection"])
def test_disabling_a_protection_needs_the_downgrade_permission(option):
    diff = diff_schemas(declared(**{option: False}), live(**{option: True}))
    finding = only(diff)
    assert finding.kind is FindingKind.CONFLICTING_TABLE_OPTION
    assert finding.remedy is Remedy.DISABLE_PROTECTION
    assert finding.required_permission is Permission.ALLOW_PROTECTION_DOWNGRADE
    assert diff.blocked(NONE_GRANTED) == (finding,)
    assert diff.actionable(NONE_GRANTED) == ()
    granted = {Permission.ALLOW_PROTECTION_DOWNGRADE: True}
    assert diff.actionable(granted) == (finding,)
    assert diff.blocked(granted) == ()


def test_switching_to_provisioned_cannot_be_applied():
    diff = diff_schemas(declared(billing_mode="PROVISIONED"), live())
    finding = only(diff)
    assert finding.kind is FindingKind.CONFLICTING_TABLE_OPTION
    assert finding.remedy is Remedy.NONE
    # No permission resolves it, and it must not pass silently either.
    assert diff.unfixable() == (finding,)
    assert diff.requires_action(ALL_GRANTED) is True


def test_declared_provisioned_acknowledges_a_provisioned_table():
    assert diff_schemas(
        declared(billing_mode="PROVISIONED"), live(billing_mode="PROVISIONED")
    ).is_clean


# ─────────────────────────── unsupported live features ───────────────────────────

LSI = {
    "partition_key": "pk",
    "sort_key": "created",
    "projection": "ALL",
    "non_key_attributes": None,
}
KMS = {"type": "KMS", "kms_key": "arn:aws:kms:us-east-1:000000000000:key/abc"}


@pytest.mark.parametrize(
    "live_schema,attribute",
    [
        (live(features={"local_secondary_indexes": {"by-created": LSI}}), "local_secondary_index"),
        (live(features={"sse": KMS}), "sse"),
        (live(features={"replicas": ["eu-west-1"]}), "replicas"),
        (
            live(features={"on_demand_throughput": {"MaxReadRequestUnits": 10}}),
            "on_demand_throughput",
        ),
        (live(stream_view_type="NEW_AND_OLD_IMAGES"), "stream_view_type"),
        (live(table_class="STANDARD_INFREQUENT_ACCESS"), "table_class"),
        (live(billing_mode="PROVISIONED"), "billing_mode"),
    ],
)
def test_unsupported_live_features_are_reported_and_left_alone(live_schema, attribute):
    diff = diff_schemas(declared(), live_schema)
    finding = only(diff)
    assert finding.kind is FindingKind.UNSUPPORTED_LIVE_FEATURE
    assert finding.severity is Severity.UNSUPPORTED
    assert finding.remedy is Remedy.NONE
    assert finding.attribute == attribute
    # On its own an unsupported feature never blocks or fails a deploy.
    assert diff.actionable(ALL_GRANTED) == ()
    assert diff.blocked(NONE_GRANTED) == ()
    assert diff.unfixable() == ()
    assert diff.requires_action(ALL_GRANTED) is False


def test_lsi_finding_names_the_index():
    diff = diff_schemas(declared(), live(features={"local_secondary_indexes": {"by-created": LSI}}))
    finding = only(diff)
    assert finding.index_name == "by-created"
    assert "unrecoverably" in finding.message


def test_a_declared_stream_is_modeled_not_unsupported():
    diff = diff_schemas(declared(stream_view_type="NEW_IMAGE"), live(stream_view_type="NEW_IMAGE"))
    assert diff.is_clean


PK_CHANGE = {"partition_key": "new_pk", "sort_key": None}


def _recreate(live_schema, **options):
    expected = declared(**options)
    expected["key_schema"] = dict(PK_CHANGE)
    return diff_schemas(expected, live_schema)


@pytest.mark.parametrize(
    "live_schema,reason",
    [
        (live(features={"local_secondary_indexes": {"by-created": LSI}}), "local secondary index"),
        (live(stream_view_type="NEW_IMAGE"), "undeclared stream"),
        (live(features={"sse": KMS}), "KMS encryption"),
        (live(features={"replicas": ["eu-west-1"]}), "replicas"),
        (live(deletion_protection=True), "deletion protection"),
    ],
)
def test_recreate_is_refused_while_the_live_table_has_something_it_would_destroy(
    live_schema, reason
):
    diff = _recreate(live_schema)
    refused = [f for f in diff.findings if f.kind is FindingKind.RECREATE_REFUSED]
    assert len(refused) == 1
    assert reason in refused[0].message
    # The refusal outranks every permission: granting them all changes nothing.
    assert refused[0] in diff.unfixable()
    assert diff.requires_action(ALL_GRANTED) is True


def test_recreate_refused_when_provisioned_billing_is_declared():
    diff = _recreate(live(billing_mode="PROVISIONED"), billing_mode="PROVISIONED")
    assert FindingKind.RECREATE_REFUSED in kinds(diff)


def test_recreate_of_a_plain_table_is_not_refused():
    diff = _recreate(live(point_in_time_recovery=True, ttl_attribute="expiresAt"))
    assert FindingKind.RECREATE_REFUSED not in kinds(diff)
    assert diff.unfixable() == ()
    recreate = diff.with_remedy(Remedy.RECREATE_TABLE)
    assert len(recreate) == 1 and recreate[0].needs_new_table


def test_a_declared_stream_does_not_refuse_a_recreate():
    diff = _recreate(live(stream_view_type="NEW_IMAGE"), stream_view_type="NEW_IMAGE")
    assert FindingKind.RECREATE_REFUSED not in kinds(diff)


def test_unsupported_features_without_a_recreate_do_not_refuse_anything():
    diff = diff_schemas(declared(), live(features={"local_secondary_indexes": {"x": LSI}}))
    assert FindingKind.RECREATE_REFUSED not in kinds(diff)


def test_option_values_serialize_to_json():
    diff = diff_schemas(declared(ttl_attribute="expiresAt"), live())
    assert only(diff).to_json() == {
        "kind": "conflicting_table_option",
        "severity": "conflict",
        "remedy": "update_options",
        "table_name": "tbl",
        "message": only(diff).message,
        "attribute": "ttl_attribute",
        "declared": "expiresAt",
    }


def test_unmanaged_sentinel_is_never_reported_as_a_value():
    diff = diff_schemas(declared(), live(ttl_attribute="expiresAt"))
    assert only(diff).declared is None
    assert UNMANAGED not in (only(diff).declared, only(diff).live)
