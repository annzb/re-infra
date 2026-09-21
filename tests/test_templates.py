from __future__ import annotations

import re
from typing import Any

import pytest

from rc_infra.env_config import BUCKET_LOGICAL_IDS
from rc_infra.templates import (
    ENVIRONMENT_TEMPLATE_PATH,
    PLATFORM_TEMPLATE_PATH,
    environment_parameters,
    import_template,
    load_template,
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
