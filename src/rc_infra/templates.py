"""Load CloudFormation templates and build the tags and output keys for them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from rc_infra.env_config import Environment

PLATFORM_TEMPLATE_PATH = Path("infra/platform.yaml")
ENVIRONMENT_TEMPLATE_PATH = Path("infra/environment.yaml")
# Comment from LLM: kept for possible later use, never deployed (see planner.stack_specs).
IDENTITY_TEMPLATE_PATH = Path("infra/identity.yaml")
PIPELINE_TEMPLATE_PATH = Path("infra/pipeline.yaml")

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


def bucket_output_keys(logical_id: str) -> tuple[str, str]:
    """The name and ARN output keys environment.yaml declares for a bucket."""
    return f"{logical_id}Name", f"{logical_id}Arn"
