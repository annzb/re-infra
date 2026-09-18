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

PK_ONLY = {"partition_key": "pk", "sort_key": None}


def gsi(partition_key="a", sort_key=None, projection=None, non_key_attributes=None):
    return {
        "partition_key": partition_key,
        "sort_key": sort_key,
        "projection": projection,
        "non_key_attributes": non_key_attributes,
    }


def schema(key_schema=None, gsis=None, attribute_types=None, billing_mode="PAY_PER_REQUEST"):
    return {
        "table_name": "tbl",
        "billing_mode": billing_mode,
        "key_schema": key_schema or dict(PK_ONLY),
        "gsis": gsis or {},
        "attribute_types": attribute_types or {},
    }


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


def test_billing_mode_mismatch_is_informational_only():
    diff = diff_schemas(schema(), schema(billing_mode="PROVISIONED"))
    finding = only(diff)
    assert finding.kind is FindingKind.UNMANAGED_BILLING_MODE
    assert finding.severity is Severity.INFO
    assert diff.actionable(ALL_GRANTED) == ()
    assert diff.blocked(ALL_GRANTED) == ()


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
