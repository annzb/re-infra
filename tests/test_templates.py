from __future__ import annotations

import re
from typing import Any

import pytest

from rc_infra.env_config import BUCKET_LOGICAL_IDS
from rc_infra.templates import (
    ENVIRONMENT_TEMPLATE_PATH,
    PLATFORM_TEMPLATE_PATH,
    environment_parameters,
    identity_logical_ids,
    import_template,
    load_template,
    platform_import_targets,
)


@pytest.fixture
def env_template() -> dict[str, Any]:
    return load_template(ENVIRONMENT_TEMPLATE_PATH)


def test_templates_use_long_form_intrinsics() -> None:
    # load_template uses yaml.safe_load, which fails on !Ref / !Sub tags.
    for path in (ENVIRONMENT_TEMPLATE_PATH, PLATFORM_TEMPLATE_PATH):
        load_template(path)


def test_every_bucket_is_declared_and_retained(env_template: dict[str, Any]) -> None:
    buckets = {logical_id for logical_id, resource in env_template["Resources"].items() if resource["Type"] == "AWS::S3::Bucket"}
    assert buckets == set(BUCKET_LOGICAL_IDS.values())
    for logical_id in buckets:
        resource = env_template["Resources"][logical_id]
        assert resource["DeletionPolicy"] == "Retain", logical_id
        assert resource["UpdateReplacePolicy"] == "Retain", logical_id
        assert "NotificationConfiguration" not in resource["Properties"], logical_id


def test_bucket_names_match_env_config_derivation(env_template: dict[str, Any], env_config: Any) -> None:
    env = env_config.get("preview3")
    for purpose, logical_id in BUCKET_LOGICAL_IDS.items():
        pattern = env_template["Resources"][logical_id]["Properties"]["BucketName"]["Fn::Sub"]
        name = re.sub(r"\$\{EnvironmentName\}", env.name, pattern).replace("${AWS::AccountId}", env.account_id)
        assert name == env.buckets[purpose]


def test_template_declares_no_dynamodb_tables(env_template: dict[str, Any]) -> None:
    assert all(r["Type"] != "AWS::DynamoDB::Table" for r in env_template["Resources"].values())


def test_parameters_match_template(env_template: dict[str, Any], env_config: Any) -> None:
    assert set(environment_parameters(env_config.get("prod"))) == set(env_template["Parameters"])


def test_import_template_contains_only_imported_resources(env_template: dict[str, Any]) -> None:
    result = import_template(env_template, ["EmbeddingsBucket", "UserCorpusBucket"])
    assert set(result["Resources"]) == {"EmbeddingsBucket", "UserCorpusBucket"}
    assert result["Resources"]["EmbeddingsBucket"]["DeletionPolicy"] == "Retain"
    assert result["Parameters"] == env_template["Parameters"]
    assert "Outputs" not in result
    # The original template is untouched.
    assert "SchemaDumpsBucket" in env_template["Resources"]


def test_import_template_rejects_unknown_resource(env_template: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="NopeBucket"):
        import_template(env_template, ["NopeBucket"])


# ── rc-platform ─────────────────────────────────────────────────────

PLATFORM = load_template(PLATFORM_TEMPLATE_PATH)


def test_every_identity_profile_has_resources(env_config: Any) -> None:
    """The <Profile>UserPool naming convention must not drift from envs.yaml."""
    for profile in env_config.identity_profiles:
        for logical_id in identity_logical_ids(profile):
            assert logical_id in PLATFORM["Resources"], f"{profile}: {logical_id} missing from platform.yaml"


def test_an_unknown_profile_fails_loudly(env_config: Any) -> None:
    from dataclasses import replace

    from rc_infra.env_config import IdentityProfile

    broken = replace(
        env_config,
        identity_profiles={"nosuch": IdentityProfile(user_pool_id="us-east-1_aaaa", client_id="c", domain="d")},
    )
    with pytest.raises(ValueError, match="nosuch"):
        platform_import_targets(broken, PLATFORM)


def test_platform_import_targets_cover_every_adoptable_resource(env_config: Any) -> None:
    targets = platform_import_targets(env_config, PLATFORM)
    by_type: dict[str, int] = {}
    for target in targets:
        by_type[target.resource_type] = by_type.get(target.resource_type, 0) + 1

    profiles = len(env_config.identity_profiles)
    assert by_type == {
        "AWS::ECR::Repository": 3,
        "AWS::IAM::Role": 1,
        "AWS::Cognito::UserPool": profiles,
        "AWS::Cognito::UserPoolClient": profiles,
        "AWS::Cognito::UserPoolDomain": profiles,
    }
    # Every target names a resource the template actually declares.
    assert {t.logical_id for t in targets} <= set(PLATFORM["Resources"])


def test_every_import_target_type_can_be_looked_up(env_config: Any) -> None:
    """A target the existence check cannot answer would silently become a CREATE."""
    from rc_infra.resources import SUPPORTED_TYPES

    assert {t.resource_type for t in platform_import_targets(env_config, PLATFORM)} <= SUPPORTED_TYPES
