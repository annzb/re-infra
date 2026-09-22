"""Existence checks for the resource types rc-platform adopts.

The planner needs to know whether a resource already exists before it can decide
between creating a stack and importing into it. Buckets have their own module;
this one covers the rest, keyed by the CloudFormation type name so the planner
never has to know which AWS service answers for a given resource.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient
    from mypy_boto3_ecr import ECRClient
    from mypy_boto3_iam import IAMClient

# The error each service raises when the resource is simply absent. Anything else --
# a permissions problem, a throttle -- must propagate, because reporting a resource
# as missing would turn an IMPORT into a CREATE and try to build a second one.
_NOT_FOUND_ERRORS = frozenset({"RepositoryNotFoundException", "NoSuchEntity", "NoSuchEntityException", "ResourceNotFoundException"})


class UnknownResourceType(Exception):
    """A type was proposed for import that this module cannot look up."""


class LiveResources:
    """Clients are built on first use: a run that imports nothing never needs them."""

    def __init__(self, session: boto3.Session) -> None:
        self._session = session
        self._ecr_client: ECRClient | None = None
        self._iam_client: IAMClient | None = None
        self._cognito_client: CognitoIdentityProviderClient | None = None

    def exists(self, resource_type: str, identifier: Mapping[str, str]) -> bool:
        try:
            check = _CHECKS[resource_type]
        except KeyError:
            raise UnknownResourceType(f"cannot check whether {resource_type} exists; extend resources.py") from None
        return _absent_is_false(lambda: check(self, identifier))

    @property
    def _ecr(self) -> ECRClient:
        if self._ecr_client is None:
            self._ecr_client = self._session.client("ecr")
        return self._ecr_client

    @property
    def _iam(self) -> IAMClient:
        if self._iam_client is None:
            self._iam_client = self._session.client("iam")
        return self._iam_client

    @property
    def _cognito(self) -> CognitoIdentityProviderClient:
        if self._cognito_client is None:
            self._cognito_client = self._session.client("cognito-idp")
        return self._cognito_client


def _repository(resources: LiveResources, identifier: Mapping[str, str]) -> bool:
    resources._ecr.describe_repositories(repositoryNames=[identifier["RepositoryName"]])
    return True


def _role(resources: LiveResources, identifier: Mapping[str, str]) -> bool:
    resources._iam.get_role(RoleName=identifier["RoleName"])
    return True


def _user_pool(resources: LiveResources, identifier: Mapping[str, str]) -> bool:
    resources._cognito.describe_user_pool(UserPoolId=identifier["UserPoolId"])
    return True


def _user_pool_client(resources: LiveResources, identifier: Mapping[str, str]) -> bool:
    resources._cognito.describe_user_pool_client(UserPoolId=identifier["UserPoolId"], ClientId=identifier["ClientId"])
    return True


def _user_pool_domain(resources: LiveResources, identifier: Mapping[str, str]) -> bool:
    # Unlike every other describe here, an absent domain is not an error: the call
    # succeeds and returns a DomainDescription with nothing in it.
    described = resources._cognito.describe_user_pool_domain(Domain=identifier["Domain"])
    return bool(described.get("DomainDescription", {}).get("Domain"))


_CHECKS: dict[str, Callable[[LiveResources, Mapping[str, str]], bool]] = {
    "AWS::ECR::Repository": _repository,
    "AWS::IAM::Role": _role,
    "AWS::Cognito::UserPool": _user_pool,
    "AWS::Cognito::UserPoolClient": _user_pool_client,
    "AWS::Cognito::UserPoolDomain": _user_pool_domain,
}

SUPPORTED_TYPES = frozenset(_CHECKS)


def _absent_is_false(call: Callable[[], bool]) -> bool:
    try:
        return call()
    except ClientError as exc:
        if exc.response["Error"]["Code"] in _NOT_FOUND_ERRORS:
            return False
        raise
