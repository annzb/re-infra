from __future__ import annotations

from typing import Any

import pytest

from rc_infra.env_config import BUCKET_LOGICAL_IDS
from rc_infra.templates import (
    ENVIRONMENT_TEMPLATE_PATH,
    IDENTITY_TEMPLATE_PATH,
    PIPELINE_TEMPLATE_PATH,
    PLATFORM_TEMPLATE_PATH,
    bucket_output_keys,
    load_template,
)

# Deployed by apply. identity.yaml and pipeline.yaml are kept but not deployed.
TEMPLATE_PATHS = (PLATFORM_TEMPLATE_PATH, ENVIRONMENT_TEMPLATE_PATH)
UNDEPLOYED_TEMPLATE_PATHS = (IDENTITY_TEMPLATE_PATH, PIPELINE_TEMPLATE_PATH)
PLATFORM = load_template(PLATFORM_TEMPLATE_PATH)

# Physical names an earlier generation of the infrastructure still holds. A template
# that asked for one could not be created while that generation exists.
LEGACY_NAMES = frozenset(
    {
        "LambdaPower",
        "rc-codebuild-deploy",
        "rc-build",
        "rc-deploy-dev",
        "rc-deploy-staging",
        "rc-deploy-prod",
        "rc-deploy-preview",
        "rc-deployment-locks",
        "rc-api",
        "rc-matching",
    }
)


@pytest.fixture
def env_template() -> dict[str, Any]:
    return load_template(ENVIRONMENT_TEMPLATE_PATH)


def test_templates_use_long_form_intrinsics() -> None:
    # load_template uses yaml.safe_load, which fails on !Ref / !Sub tags.
    for path in (*TEMPLATE_PATHS, *UNDEPLOYED_TEMPLATE_PATHS):
        load_template(path)


@pytest.mark.parametrize("path", TEMPLATE_PATHS, ids=str)
def test_every_resource_is_retained(path: Any) -> None:
    # Retained on deletion; removed only by the rollback of its own first create.
    allowed = {"RetainExceptOnCreate"}
    for logical_id, resource in load_template(path)["Resources"].items():
        assert resource["DeletionPolicy"] in allowed, logical_id
        assert resource["UpdateReplacePolicy"] == "Retain", logical_id


@pytest.mark.parametrize("path", TEMPLATE_PATHS, ids=str)
def test_no_legacy_physical_names(path: Any) -> None:
    def strings(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [s for item in value.values() for s in strings(item)]
        if isinstance(value, list):
            return [s for item in value for s in strings(item)]
        return [value] if isinstance(value, str) else []

    assert not set(strings(load_template(path)["Resources"])) & LEGACY_NAMES


def test_every_bucket_is_declared_with_a_generated_name(env_template: dict[str, Any]) -> None:
    buckets = {logical_id for logical_id, resource in env_template["Resources"].items() if resource["Type"] == "AWS::S3::Bucket"}
    assert buckets == set(BUCKET_LOGICAL_IDS.values())
    for logical_id in buckets:
        properties = env_template["Resources"][logical_id]["Properties"]
        assert "BucketName" not in properties, logical_id
        assert "NotificationConfiguration" not in properties, logical_id


def test_every_bucket_has_name_and_arn_outputs(env_template: dict[str, Any]) -> None:
    for logical_id in BUCKET_LOGICAL_IDS.values():
        name, arn = bucket_output_keys(logical_id)
        assert env_template["Outputs"][name]["Value"] == {"Ref": logical_id}
        assert env_template["Outputs"][arn]["Value"] == {"Fn::GetAtt": [logical_id, "Arn"]}


def test_environment_template_takes_no_parameters(env_template: dict[str, Any]) -> None:
    assert "Parameters" not in env_template


def test_template_declares_no_dynamodb_tables(env_template: dict[str, Any]) -> None:
    assert all(r["Type"] != "AWS::DynamoDB::Table" for r in env_template["Resources"].values())


# ── rc-platform ─────────────────────────────────────────────────────


def test_platform_declares_no_identity_resources() -> None:
    assert not any(r["Type"].startswith("AWS::Cognito::") for r in PLATFORM["Resources"].values())


def test_shared_role_name_is_generated() -> None:
    role = PLATFORM["Resources"]["SharedLambdaExecutionRole"]
    assert "RoleName" not in role["Properties"]
    assert "SharedLambdaExecutionRoleArn" in PLATFORM["Outputs"]


def test_platform_outputs_every_repository_uri() -> None:
    repositories = [logical_id for logical_id, r in PLATFORM["Resources"].items() if r["Type"] == "AWS::ECR::Repository"]
    assert {f"{logical_id}Uri" for logical_id in repositories} <= set(PLATFORM["Outputs"])


# Prefixes retribalize-core presigns because they are private (aws_utils/s3.py presign_get).
PRIVATE_AVATAR_PREFIXES = ("event-photos/", "residency-photos/", "service-photos/", "project-documents/", "event-documents/")


def test_avatar_policy_opens_only_the_public_prefixes(env_template: dict[str, Any]) -> None:
    policy = env_template["Resources"]["AvatarsBucketPolicy"]["Properties"]
    assert policy["Bucket"] == {"Ref": "AvatarsBucket"}
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert statement["Action"] == "s3:GetObject"
    resources = [r["Fn::Sub"] for r in statement["Resource"]]
    assert all(r.startswith("${AvatarsBucket.Arn}/") and r.endswith("/*") for r in resources)
    assert "${AvatarsBucket.Arn}/*" not in resources
    assert not [r for r in resources for prefix in PRIVATE_AVATAR_PREFIXES if prefix in r]
