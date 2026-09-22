from __future__ import annotations

import pytest

from rc_infra.buckets import config_diff
from rc_infra.env_config import BUCKET_LOGICAL_IDS
from rc_infra.templates import ENVIRONMENT_TEMPLATE_PATH, bucket_properties, load_template
from tests.fakes import matching_live_config

TEMPLATE = load_template(ENVIRONMENT_TEMPLATE_PATH)


@pytest.mark.parametrize("purpose", sorted(BUCKET_LOGICAL_IDS))
def test_matching_live_bucket_has_no_diff(purpose: str) -> None:
    assert config_diff(bucket_properties(TEMPLATE, purpose), matching_live_config(purpose)) == []


def test_missing_lifecycle_is_a_diff() -> None:
    live = matching_live_config("user-corpus")
    live["lifecycle"] = None
    diffs = config_diff(bucket_properties(TEMPLATE, "user-corpus"), live)
    assert [(d.group, d.blocking) for d in diffs] == [("lifecycle", True)]


def test_extra_live_cors_is_a_diff() -> None:
    live = matching_live_config("avatars")
    live["cors"] = {"CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["*"]}]}
    # CORS is advisory: the import proceeds and the following update reconciles it.
    assert [(d.group, d.blocking) for d in config_diff(bucket_properties(TEMPLATE, "avatars"), live)] == [("cors", False)]


def test_versioning_enabled_live_is_a_diff() -> None:
    live = matching_live_config("embeddings")
    live["versioning"] = {"Status": "Enabled"}
    assert [(d.group, d.blocking) for d in config_diff(bucket_properties(TEMPLATE, "embeddings"), live)] == [("versioning", True)]


def test_missing_public_access_block_is_a_diff() -> None:
    live = matching_live_config("recordings")
    live["public_access_block"] = None
    assert [(d.group, d.blocking) for d in config_diff(bucket_properties(TEMPLATE, "recordings"), live)] == [("public_access_block", True)]


def test_legacy_prefix_field_and_rule_order_are_normalized() -> None:
    live = matching_live_config("user-corpus")
    live["lifecycle"] = {
        "Rules": [
            {
                "ID": "ExpireRawUploads",
                "Status": "Enabled",
                "Prefix": "uploads/",
                "Expiration": {"Days": 7},
            }
        ]
    }
    live["cors"]["CORSRules"][0]["AllowedHeaders"] = ["*"]
    assert config_diff(bucket_properties(TEMPLATE, "user-corpus"), live) == []


def test_unmodelled_live_lifecycle_settings_are_a_diff() -> None:
    live = matching_live_config("user-corpus")
    live["lifecycle"]["Rules"][0]["Transitions"] = [{"Days": 30, "StorageClass": "GLACIER"}]
    assert [(d.group, d.blocking) for d in config_diff(bucket_properties(TEMPLATE, "user-corpus"), live)] == [("lifecycle", True)]


def test_unsupported_template_property_fails_loudly() -> None:
    with pytest.raises(ValueError, match="OwnershipControls"):
        config_diff({"OwnershipControls": {}}, matching_live_config("avatars"))


def test_public_avatars_bucket_must_not_be_blocked() -> None:
    """Blocking public access would reject the policy that serves every avatar."""
    live = matching_live_config("avatars")
    live["public_access_block"]["PublicAccessBlockConfiguration"]["BlockPublicPolicy"] = True
    assert [(d.group, d.blocking) for d in config_diff(bucket_properties(TEMPLATE, "avatars"), live)] == [("public_access_block", True)]
