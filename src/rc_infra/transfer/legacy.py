"""Where the generation of buckets that predates re-infra kept its data.

The only knowledge of that generation in this repository. The normal deployment path
never imports this module (tests/test_architecture.py enforces it).

The old layout is not one naming formula, so it is spelled out per resource, as
retribalize-core's template-v2.yaml mapped it and as the read-only AWS audit of
2026-10-05 (scripts/aws-audit.sh) found it:

  - prod, staging and dev had their own buckets: rc-<env>-<purpose>-<account>.
  - Every preview slot shared the preview tier's buckets: rc-preview-<purpose>-<account>,
    except preview67's and preview89's property registries, which were their own.
  - Two buckets core declares were never created, so there is nothing to copy:
    rc-dev-recordings and rc-preview67-property-registry.
  - Schema dumps had no bucket of their own (core fell back to a prefix in the
    embeddings bucket), so they have no legacy source either.

Several previews resolving to one shared source is deliberate: which slot should
receive the shared preview data is a data-migration decision, not a naming one.
rc-preview1-avatars and rc-preview2-avatars exist but nothing ever referenced them;
they are not a source of anything.
"""

from __future__ import annotations

import re

from rc_infra.transfer.arns import bucket_arn

# Slots that had dedicated buckets, and the slots whose property registry was their own.
_DEDICATED = frozenset({"prod", "staging", "dev"})
_OWN_PROPERTY_REGISTRY = frozenset({"preview67", "preview89"})
_PREVIEW_TIER = "preview"
_PREVIEW_SLOT = re.compile(r"^preview[0-9]{1,8}$")

# (tier, purpose) pairs core's template names but the audit found no bucket for.
_NEVER_CREATED = frozenset({("dev", "recordings"), ("preview67", "property-registry")})


class NoLegacySource(ValueError):
    """The old generation had no data for this environment and purpose."""


def tier(environment: str, purpose: str) -> str:
    """The old persistence tier whose bucket held an environment's data for a purpose."""
    if environment in _DEDICATED:
        return environment
    if environment == _PREVIEW_TIER:
        return _PREVIEW_TIER
    if _PREVIEW_SLOT.match(environment):
        if purpose == "property-registry" and environment in _OWN_PROPERTY_REGISTRY:
            return environment
        return _PREVIEW_TIER
    raise ValueError(f"{environment!r} is neither a legacy environment nor a preview slot")


def bucket(environment: str, purpose: str, account_id: str) -> str:
    """The ARN of the legacy bucket that held an environment's data for a purpose."""
    if purpose == "schema-dumps":
        raise NoLegacySource("schema dumps had no bucket of their own in the old generation (they were a prefix of the embeddings bucket)")
    resolved = tier(environment, purpose)
    if (resolved, purpose) in _NEVER_CREATED:
        raise NoLegacySource(f"rc-{resolved}-{purpose} was declared by retribalize-core but never created; there is nothing to copy")
    return bucket_arn(f"rc-{resolved}-{purpose}-{account_id}")
