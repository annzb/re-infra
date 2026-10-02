"""Where the current, re-infra-managed generation keeps an environment's data: its stack outputs."""

from __future__ import annotations

from rc_infra.aws import StackApi
from rc_infra.env_config import BUCKET_LOGICAL_IDS, EnvConfig
from rc_infra.templates import bucket_output_keys


class Unresolved(Exception):
    pass


def bucket(config: EnvConfig, stacks: StackApi, environment: str, purpose: str) -> str:
    """The ARN of an environment's bucket, read from its rc-env-<environment> stack outputs."""
    if environment not in config.names:
        raise Unresolved(f"{environment!r} is not declared in envs.yaml")
    env = config.get(environment)
    stack = stacks.get_stack(env.core_stack)
    if stack is None:
        raise Unresolved(f"stack {env.core_stack} does not exist; run `rc-infra apply` first")
    key = bucket_output_keys(BUCKET_LOGICAL_IDS[purpose])[1]
    if key not in stack.outputs:
        raise Unresolved(f"stack {env.core_stack} has no output {key}")
    return stack.outputs[key]
