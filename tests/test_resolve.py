from __future__ import annotations

import pytest

from rc_infra import aws as aws_module
from rc_infra.env_config import EnvConfig
from rc_infra.transfer import legacy, resolve_current
from rc_infra.transfer.cli import main
from tests.fakes import FakeAws, bucket_name, env_stack_outputs, env_stack_tags
from tests.transfer_fakes import FakeBucket, FakeClients


def test_legacy_bucket_follows_the_old_naming() -> None:
    assert legacy.bucket("preview", "user-corpus", "273268178059") == "arn:aws:s3:::rc-preview-user-corpus-273268178059"
    with pytest.raises(ValueError):
        legacy.bucket("Bad-Name", "avatars", "273268178059")


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
    clients.s3_client.buckets["rc-preview67-user-corpus-273268178059"] = FakeBucket()
    clients.s3_client.buckets[bucket_name("dev", "user-corpus")] = FakeBucket()
    clients.s3_client.put("rc-preview67-user-corpus-273268178059", "uploads/a.txt")

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
    assert "source: arn:aws:s3:::rc-preview67-user-corpus-273268178059" in out
    source, target = "arn:aws:s3:::rc-preview67-user-corpus-273268178059", f"arn:aws:s3:::{bucket_name('dev', 'user-corpus')}"
    assert f"equivalent: rc-data-transfer bucket --source {source} --target {target} --dry-run" in out
    assert clients.s3_client.copies == []
