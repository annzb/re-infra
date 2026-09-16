"""boto3 factories: configuration and caching, without network access."""

from rc_lambda_base import aws
from rc_lambda_base.settings import Settings


def test_session_kwargs_include_endpoint_only_when_set():
    assert aws.session_kwargs(Settings()) == {"region_name": "us-east-1"}
    assert aws.session_kwargs(
        Settings(aws_region="eu-west-1", aws_endpoint_url="http://localhost:4566")
    ) == {
        "region_name": "eu-west-1",
        "endpoint_url": "http://localhost:4566",
    }


def test_resources_are_cached_per_settings():
    aws.clear_cache()
    local = Settings(aws_endpoint_url="http://localhost:4566")

    assert aws.dynamodb_resource(local) is aws.dynamodb_resource(
        Settings(aws_endpoint_url="http://localhost:4566")
    )
    assert aws.dynamodb_resource(local) is not aws.dynamodb_resource(Settings())
    assert aws.client("s3", local).meta.endpoint_url == "http://localhost:4566"
    aws.clear_cache()
