from __future__ import annotations

import pytest

from rc_infra import aws as aws_module
from rc_infra.env_config import EnvConfig
from rc_infra.transfer import legacy, resolve_current
from rc_infra.transfer.cli import main
from tests.fakes import FakeAws, bucket_name, env_stack_outputs, env_stack_tags
from tests.transfer_fakes import FakeBucket, FakeClients

ACCOUNT = "273268178059"


@pytest.mark.parametrize(
    ("environment", "purpose", "expected"),
    [
        ("prod", "avatars", "rc-prod-avatars"),
        ("dev", "property-registry", "rc-dev-property-registry"),
        ("preview", "user-corpus", "rc-preview-user-corpus"),
        ("preview3", "embeddings", "rc-preview-embeddings"),
        ("preview89", "avatars", "rc-preview-avatars"),
        ("preview89", "property-registry", "rc-preview89-property-registry"),
        ("preview3", "property-registry", "rc-preview-property-registry"),
    ],
)
def test_legacy_layout(environment: str, purpose: str, expected: str) -> None:
    assert legacy.bucket(environment, purpose, ACCOUNT) == f"arn:aws:s3:::{expected}-{ACCOUNT}"


@pytest.mark.parametrize(("environment", "purpose"), [("dev", "recordings"), ("preview67", "property-registry"), ("prod", "schema-dumps")])
def test_legacy_resources_that_never_existed(environment: str, purpose: str) -> None:
    with pytest.raises(legacy.NoLegacySource):
        legacy.bucket(environment, purpose, ACCOUNT)


def test_unknown_legacy_environment() -> None:
    with pytest.raises(ValueError):
        legacy.bucket("Bad-Name", "avatars", ACCOUNT)


def test_current_bucket_comes_from_stack_outputs(env_config: EnvConfig, fake: FakeAws) -> None:
    fake.stacks.add("rc-env-dev", tags=env_stack_tags("dev"), outputs=env_stack_outputs("dev"))

    assert resolve_current.bucket(env_config, fake.stacks, "dev", "avatars") == f"arn:aws:s3:::{bucket_name('dev', 'avatars')}"


@pytest.mark.parametrize(("environment", "message"), [("nope", "not declared"), ("staging", "does not exist")])
def test_current_bucket_fails_loudly(env_config: EnvConfig, fake: FakeAws, environment: str, message: str) -> None:
    with pytest.raises(resolve_current.Unresolved, match=message):
        resolve_current.bucket(env_config, fake.stacks, environment, "avatars")


def test_resolve_copy_prints_the_primitive_and_runs_it(fake: FakeAws, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake.stacks.add("rc-env-dev", tags=env_stack_tags("dev"), outputs=env_stack_outputs("dev"))
    monkeypatch.setattr(aws_module, "connect", lambda region: fake.aws)
    clients = FakeClients()
    clients.s3_client.buckets["rc-preview-user-corpus-273268178059"] = FakeBucket()
    clients.s3_client.buckets[bucket_name("dev", "user-corpus")] = FakeBucket()
    clients.s3_client.put("rc-preview-user-corpus-273268178059", "uploads/a.txt")

    code = main(
        [
            "resolve-copy",
            "--source-layout",
            "legacy",
            "--source-env",
            "preview67",
            "--source-resource",
            "UserCorpusBucket",
            "--target-env",
            "dev",
            "--target-resource",
            "user-corpus",
            "--dry-run",
        ],
        clients=clients,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "source: arn:aws:s3:::rc-preview-user-corpus-273268178059" in out
    source, target = "arn:aws:s3:::rc-preview-user-corpus-273268178059", f"arn:aws:s3:::{bucket_name('dev', 'user-corpus')}"
    assert f"equivalent: rc-data-transfer bucket --source {source} --target {target} --dry-run" in out
    assert clients.s3_client.copies == []
