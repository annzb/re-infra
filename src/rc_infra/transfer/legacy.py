"""Where the generation of buckets that predates re-infra kept its data.

The only knowledge of that generation in this repository. The normal deployment path
never imports this module (tests/test_architecture.py enforces it).
"""

from __future__ import annotations

import re

from rc_infra.transfer.arns import bucket_arn

# Legacy environment names include persistence tiers that are not deployment slots
# today, such as the shared "preview" tier, so they are not checked against envs.yaml.
_LEGACY_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9]{1,19}$")


def bucket(environment: str, purpose: str, account_id: str) -> str:
    """The ARN of a legacy bucket: rc-<environment>-<purpose>-<account>."""
    if not _LEGACY_ENVIRONMENT.match(environment):
        raise ValueError(f"{environment!r} is not a legacy environment name")
    return bucket_arn(f"rc-{environment}-{purpose}-{account_id}")
