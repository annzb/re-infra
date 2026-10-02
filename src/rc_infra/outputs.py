"""The deployment contract: every physical identifier retribalize-core needs, read from stack outputs.

Nothing here is derived from a naming convention. If a stack or output is missing,
the contract is incomplete and resolving it fails, rather than guessing a name.
"""

from __future__ import annotations

from typing import Any

from rc_infra.aws import Aws
from rc_infra.env_config import BUCKET_LOGICAL_IDS, IDENTITY_STACK_NAME, PLATFORM_STACK_NAME, Environment
from rc_infra.templates import bucket_output_keys, identity_output_keys

# Contract field -> rc-platform output key.
_PLATFORM_OUTPUTS = {
    "lambda_execution_role_arn": "SharedLambdaExecutionRoleArn",
    "base_repository_uri": "LambdaBaseImageRepositoryUri",
    "api_repository_uri": "ApiImageRepositoryUri",
    "matching_repository_uri": "MatchingImageRepositoryUri",
}


class OutputsUnavailable(Exception):
    pass


def environment_contract(env: Environment, aws: Aws) -> dict[str, Any]:
    platform = _stack_outputs(aws, PLATFORM_STACK_NAME)
    identity = _stack_outputs(aws, IDENTITY_STACK_NAME)
    environment = _stack_outputs(aws, env.core_stack)

    pool, client, domain = identity_output_keys(env.identity_profile)
    buckets = {}
    for purpose, logical_id in BUCKET_LOGICAL_IDS.items():
        name, arn = bucket_output_keys(logical_id)
        buckets[purpose] = {"name": _require(environment, env.core_stack, name), "arn": _require(environment, env.core_stack, arn)}
    return {
        "environment": env.name,
        "buckets": buckets,
        "identity": {
            "profile": env.identity_profile,
            "user_pool_id": _require(identity, IDENTITY_STACK_NAME, pool),
            "client_id": _require(identity, IDENTITY_STACK_NAME, client),
            "oauth_domain": _require(identity, IDENTITY_STACK_NAME, domain),
        },
        "platform": {field: _require(platform, PLATFORM_STACK_NAME, key) for field, key in _PLATFORM_OUTPUTS.items()},
    }


def _stack_outputs(aws: Aws, stack_name: str) -> dict[str, str]:
    stack = aws.stacks.get_stack(stack_name)
    if stack is None:
        raise OutputsUnavailable(f"stack {stack_name} does not exist; run `rc-infra apply` first")
    if stack.is_busy or stack.is_broken or stack.is_pending_review or stack.is_failed_create:
        raise OutputsUnavailable(f"stack {stack_name} is {stack.status}; its outputs cannot be trusted")
    return dict(stack.outputs)


def _require(outputs: dict[str, str], stack_name: str, key: str) -> str:
    try:
        return outputs[key]
    except KeyError:
        raise OutputsUnavailable(f"stack {stack_name} has no output {key}") from None
