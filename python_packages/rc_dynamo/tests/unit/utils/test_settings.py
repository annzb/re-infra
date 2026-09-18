"""Settings parsing: defaults, booleans, numbers, and the legacy-flag guard."""

import pytest

from rc_dynamo.utils.settings import Settings, SettingsError


def test_defaults_when_nothing_is_set():
    assert Settings.from_env({}) == Settings()


def test_reads_every_setting():
    settings = Settings.from_env(
        {
            "AWS_REGION": "eu-west-1",
            "AWS_ENDPOINT_URL": "http://localhost:4566",
            "DYNAMO_SCHEMA_POLL_SECONDS": "1",
            "DYNAMO_SCHEMA_WAIT_TIMEOUT_SECONDS": "30.5",
            "DYNAMO_PRUNE_UNDECLARED": "true",
            "DYNAMO_ALLOW_TABLE_RECREATE": "yes",
        }
    )

    assert settings == Settings(
        aws_region="eu-west-1",
        aws_endpoint_url="http://localhost:4566",
        schema_poll_seconds=1.0,
        schema_wait_timeout_seconds=30.5,
        prune_undeclared=True,
        allow_table_recreate=True,
    )


def test_region_falls_back_to_aws_default_region():
    assert Settings.from_env({"AWS_DEFAULT_REGION": "us-west-2"}).aws_region == "us-west-2"


def test_blank_values_use_defaults():
    settings = Settings.from_env(
        {"AWS_ENDPOINT_URL": " ", "DYNAMO_PRUNE_UNDECLARED": "", "AWS_REGION": ""}
    )
    assert settings.aws_endpoint_url is None
    assert settings.prune_undeclared is False
    assert settings.aws_region == "us-east-1"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "y", "on"])
def test_truthy_booleans(raw):
    assert Settings.from_env({"DYNAMO_PRUNE_UNDECLARED": raw}).prune_undeclared is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "n", "off"])
def test_falsy_booleans(raw):
    assert Settings.from_env({"DYNAMO_ALLOW_TABLE_RECREATE": raw}).allow_table_recreate is False


def test_invalid_boolean_is_rejected_rather_than_read_as_false():
    with pytest.raises(SettingsError, match="DYNAMO_ALLOW_TABLE_RECREATE"):
        Settings.from_env({"DYNAMO_ALLOW_TABLE_RECREATE": "maybe"})


@pytest.mark.parametrize("raw", ["soon", "0", "-1", "nan", "inf"])
def test_invalid_poll_interval_is_rejected(raw):
    with pytest.raises(SettingsError, match="DYNAMO_SCHEMA_POLL_SECONDS"):
        Settings.from_env({"DYNAMO_SCHEMA_POLL_SECONDS": raw})


def test_invalid_wait_timeout_is_rejected():
    with pytest.raises(SettingsError, match="DYNAMO_SCHEMA_WAIT_TIMEOUT_SECONDS"):
        Settings.from_env({"DYNAMO_SCHEMA_WAIT_TIMEOUT_SECONDS": "ten"})


def test_renamed_flag_fails_instead_of_going_inert():
    with pytest.raises(SettingsError, match="DYNAMO_ALLOW_GSI_DELETE"):
        Settings.from_env({"DYNAMO_ALLOW_GSI_DELETE": "true"})
