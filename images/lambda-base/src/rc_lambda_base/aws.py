"""boto3 factories configured from :class:`~rc_lambda_base.settings.Settings`.

Nothing here runs at import time. Resources and clients are cached per service
and settings value, so every table declaration shares one connection pool.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Optional

import boto3

from rc_lambda_base.settings import Settings


def session_kwargs(settings: Settings) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"region_name": settings.aws_region}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return kwargs


@lru_cache(maxsize=None)
def _resource(service: str, settings: Settings) -> Any:
    return boto3.resource(service, **session_kwargs(settings))


@lru_cache(maxsize=None)
def _client(service: str, settings: Settings) -> Any:
    return boto3.client(service, **session_kwargs(settings))


def resource(service: str, settings: Optional[Settings] = None) -> Any:
    return _resource(service, settings if settings is not None else Settings.from_env())


def client(service: str, settings: Optional[Settings] = None) -> Any:
    return _client(service, settings if settings is not None else Settings.from_env())


def dynamodb_resource(settings: Optional[Settings] = None) -> Any:
    return resource("dynamodb", settings)


def clear_cache() -> None:
    _resource.cache_clear()
    _client.cache_clear()
