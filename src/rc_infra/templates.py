"""Load CloudFormation templates and build the parameters, tags, and import templates for them."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from rc_infra.aws import ImportTarget
from rc_infra.env_config import BUCKET_LOGICAL_IDS, EnvConfig, Environment

PLATFORM_TEMPLATE_PATH = Path("infra/platform.yaml")
ENVIRONMENT_TEMPLATE_PATH = Path("infra/environment.yaml")

# Logical IDs in platform.yaml whose resource already existed before this repository
# did, mapped to the template property that carries their CloudFormation identifier.
# Everything here is adopted by import, never created.
_SELF_IDENTIFIED_PLATFORM_RESOURCES: dict[str, tuple[str, str]] = {
    "LambdaBaseImageRepository": ("AWS::ECR::Repository", "RepositoryName"),
    "ApiImageRepository": ("AWS::ECR::Repository", "RepositoryName"),
    "MatchingImageRepository": ("AWS::ECR::Repository", "RepositoryName"),
    "LambdaPowerRole": ("AWS::IAM::Role", "RoleName"),
}

MANAGED_BY_TAG = "ManagedBy"
MANAGED_BY_VALUE = "re-infra"
COMPONENT_TAG = "Component"
ENVIRONMENT_TAG = "Environment"


def load_template(path: Path) -> dict[str, Any]:
    template = yaml.safe_load(path.read_text())
    if not isinstance(template, dict) or "Resources" not in template:
        raise ValueError(f"{path}: not a CloudFormation template")
    return template


def template_body(template: dict[str, Any]) -> str:
    return json.dumps(template, indent=1)


def platform_tags() -> dict[str, str]:
    return {MANAGED_BY_TAG: MANAGED_BY_VALUE, COMPONENT_TAG: "platform"}


def environment_tags(env: Environment) -> dict[str, str]:
    return {
        MANAGED_BY_TAG: MANAGED_BY_VALUE,
        COMPONENT_TAG: "environment",
        ENVIRONMENT_TAG: env.name,
    }


def environment_parameters(env: Environment) -> dict[str, str]:
    return {"EnvironmentName": env.name}


def import_template(template: dict[str, Any], logical_ids: Iterable[str]) -> dict[str, Any]:
    """The template for an IMPORT change set: only the resources being imported.

    An import may not create or modify anything else, so SSM parameters, outputs,
    and buckets that do not exist yet are added by the UPDATE that follows.
    """
    ids = list(logical_ids)
    missing = [logical_id for logical_id in ids if logical_id not in template["Resources"]]
    if missing:
        raise ValueError(f"template has no resources named {missing}")
    result: dict[str, Any] = {
        key: copy.deepcopy(template[key]) for key in ("AWSTemplateFormatVersion", "Description", "Parameters") if key in template
    }
    result["Resources"] = {logical_id: copy.deepcopy(template["Resources"][logical_id]) for logical_id in ids}
    return result


def identity_logical_ids(profile: str) -> tuple[str, str, str]:
    """The pool, client and domain logical IDs platform.yaml declares for a profile."""
    prefix = profile.capitalize()
    return f"{prefix}UserPool", f"{prefix}UserPoolClient", f"{prefix}UserPoolDomain"


def platform_import_targets(config: EnvConfig, template: dict[str, Any]) -> list[ImportTarget]:
    """Every rc-platform resource that can be adopted rather than created.

    The caller filters these by what actually exists: a resource listed here and
    absent from AWS is simply created by the update that follows the import.
    """
    resources = template["Resources"]
    targets: list[ImportTarget] = []

    for logical_id, (resource_type, property_name) in _SELF_IDENTIFIED_PLATFORM_RESOURCES.items():
        if logical_id not in resources:
            continue
        name = resources[logical_id]["Properties"][property_name]
        targets.append(ImportTarget(logical_id, resource_type, {property_name: name}, name))

    for profile_name, profile in sorted(config.identity_profiles.items()):
        pool_id, client_id, domain_id = identity_logical_ids(profile_name)
        missing = [logical_id for logical_id in (pool_id, client_id, domain_id) if logical_id not in resources]
        if missing:
            raise ValueError(f"identity profile {profile_name!r} has no {missing} in {PLATFORM_TEMPLATE_PATH}")
        # The pool and client IDs were assigned by AWS and are recorded in envs.yaml.
        # The domain prefix is a template property; envs.yaml holds the full hostname.
        pool = {"UserPoolId": profile.user_pool_id}
        domain = resources[domain_id]["Properties"]["Domain"]
        targets.append(ImportTarget(pool_id, "AWS::Cognito::UserPool", pool, profile.user_pool_id))
        targets.append(ImportTarget(client_id, "AWS::Cognito::UserPoolClient", {**pool, "ClientId": profile.client_id}, profile.client_id))
        targets.append(ImportTarget(domain_id, "AWS::Cognito::UserPoolDomain", {**pool, "Domain": domain}, domain))

    return targets


def bucket_properties(template: dict[str, Any], purpose: str) -> dict[str, Any]:
    """The declared configuration of a bucket, excluding its (derived) name."""
    resource = template["Resources"][BUCKET_LOGICAL_IDS[purpose]]
    properties = dict(resource.get("Properties", {}))
    properties.pop("BucketName", None)
    return properties
