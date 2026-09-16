"""Load CloudFormation templates and build the parameters, tags, and import templates for them."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from rc_infra.env_config import BUCKET_LOGICAL_IDS, Environment

PLATFORM_TEMPLATE_PATH = Path("infra/platform.yaml")
ENVIRONMENT_TEMPLATE_PATH = Path("infra/environment.yaml")

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
    return {
        "EnvironmentName": env.name,
        "IdentityProfile": env.identity_profile,
        "UserPoolId": env.identity.user_pool_id,
        "UserPoolClientId": env.identity.client_id,
        "CognitoDomain": env.identity.domain,
    }


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


def bucket_properties(template: dict[str, Any], purpose: str) -> dict[str, Any]:
    """The declared configuration of a bucket, excluding its (derived) name."""
    resource = template["Resources"][BUCKET_LOGICAL_IDS[purpose]]
    properties = dict(resource.get("Properties", {}))
    properties.pop("BucketName", None)
    return properties
