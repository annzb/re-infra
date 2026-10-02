from __future__ import annotations

import pytest

from rc_infra.env_config import EnvConfig
from rc_infra.outputs import OutputsUnavailable, environment_contract
from tests.fakes import FakeAws, bucket_name, env_stack_outputs, env_stack_tags

PLATFORM_OUTPUTS = {
    "SharedLambdaExecutionRoleArn": "arn:aws:iam::273268178059:role/rc-platform-SharedLambdaExecutionRole-XYZ",
    "LambdaBaseImageRepositoryUri": "273268178059.dkr.ecr.us-east-1.amazonaws.com/rc-lambda-base",
    "ApiImageRepositoryUri": "273268178059.dkr.ecr.us-east-1.amazonaws.com/rc-api-v2",
    "MatchingImageRepositoryUri": "273268178059.dkr.ecr.us-east-1.amazonaws.com/rc-matching-v2",
}
IDENTITY_OUTPUTS = {
    "PreviewUserPoolId": "us-east-1_new",
    "PreviewUserPoolClientId": "client",
    "PreviewOAuthDomain": "rc-preview-v3.auth.us-east-1.amazoncognito.com",
}


def _deployed(fake: FakeAws, environment: str) -> None:
    fake.stacks.add("rc-platform", outputs=PLATFORM_OUTPUTS)
    fake.stacks.add("rc-identity", outputs=IDENTITY_OUTPUTS)
    fake.stacks.add(f"rc-env-{environment}", tags=env_stack_tags(environment), outputs=env_stack_outputs(environment))


def test_contract_comes_from_stack_outputs(env_config: EnvConfig, fake: FakeAws) -> None:
    _deployed(fake, "preview3")

    contract = environment_contract(env_config.get("preview3"), fake.aws)

    assert contract["environment"] == "preview3"
    assert contract["buckets"]["user-corpus"] == {
        "name": bucket_name("preview3", "user-corpus"),
        "arn": f"arn:aws:s3:::{bucket_name('preview3', 'user-corpus')}",
    }
    assert contract["identity"] == {
        "profile": "preview",
        "user_pool_id": "us-east-1_new",
        "client_id": "client",
        "oauth_domain": "rc-preview-v3.auth.us-east-1.amazoncognito.com",
    }
    assert contract["platform"]["lambda_execution_role_arn"] == PLATFORM_OUTPUTS["SharedLambdaExecutionRoleArn"]


def test_missing_stack_fails_loudly(env_config: EnvConfig, fake: FakeAws) -> None:
    with pytest.raises(OutputsUnavailable, match="rc-platform does not exist"):
        environment_contract(env_config.get("dev"), fake.aws)


def test_missing_output_fails_loudly(env_config: EnvConfig, fake: FakeAws) -> None:
    _deployed(fake, "preview3")
    fake.stacks.add("rc-identity", outputs={k: v for k, v in IDENTITY_OUTPUTS.items() if k != "PreviewUserPoolClientId"})

    with pytest.raises(OutputsUnavailable, match="PreviewUserPoolClientId"):
        environment_contract(env_config.get("preview3"), fake.aws)


def test_busy_stack_is_not_trusted(env_config: EnvConfig, fake: FakeAws) -> None:
    _deployed(fake, "preview3")
    fake.stacks.add("rc-env-preview3", status="UPDATE_ROLLBACK_FAILED", outputs=env_stack_outputs("preview3"))

    with pytest.raises(OutputsUnavailable, match="UPDATE_ROLLBACK_FAILED"):
        environment_contract(env_config.get("preview3"), fake.aws)
