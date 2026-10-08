"""The contract registry: CodeArtifact domain, repository, reference and IAM fragments (design D3, D16; CS-04).

FinancialPlanning owns the registry (ownership matrix row ``contract-registry``, account-level
``shared``) and declares it in its tooling stack:

* CodeArtifact domain :data:`DOMAIN` and repository :data:`REPOSITORY` (no upstream: the
  repository holds only finplan packages, so an install that names it explicitly can never
  resolve a public package of the same name);
* the reference ``/finplan/shared/financialplanning/contract/registry-ref``
  (:data:`REGISTRY_REF_PARAMETER`), whose value is the JSON text built by
  :func:`registry_ref_value` (domain, repository, region and formats; never an account ID or an
  endpoint URL). Consumers parse it with :func:`parse_registry_ref`; the domain owner is their own
  account (single account, D5).

Packages: the Python distribution :data:`PYPI_PACKAGE` (format ``pypi``) and the npm package
``@finplan/contracts`` (format ``npm``, namespace :data:`NPM_NAMESPACE`, name :data:`NPM_PACKAGE`).
Published versions are immutable: the FinancialPlanning publish step refuses to publish a version
that already exists unless the stored asset has the same SHA-256 (then it is a no-op), and no
role is granted a delete, dispose or status-change action.

IAM fragments (ARNs from the CloudFormation pseudo parameters, never an account literal):

* :func:`read_policy`: what a consumer build role needs to install a pinned version
  (``codeartifact:GetAuthorizationToken`` on the domain, ``GetRepositoryEndpoint`` and
  ``ReadFromRepository`` on the repository, ``sts:GetServiceBearerToken`` for CodeArtifact only, and
  a read of the registry reference). Each consumer repository attaches it to its own build roles.
* :func:`publish_policy`: the FinancialPlanning build role's grant, scoped to this one repository
  and its packages (read plus ``PublishPackageVersion``, ``PutPackageMetadata`` and the version
  reads the publish step uses to refuse overwrites).
* :func:`domain_policy` / :func:`repository_policy`: the resource policies on the domain and the
  repository. They admit the build roles of the other repositories by name pattern
  (:func:`reader_role_patterns`: ``finplan-shared-<repo>-*``) for reads only, in this account only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import ssm as _ssm
from .iam import ACCOUNT, PARTITION, REGION, PolicyDocument, ssm_parameter_arn

__all__ = [
    "DOMAIN",
    "FORMATS",
    "NPM_NAMESPACE",
    "NPM_PACKAGE",
    "OWNER_REPO",
    "PUBLISH_ACTIONS",
    "PYPI_PACKAGE",
    "READ_ACTIONS",
    "REGISTRY_REF_PARAMETER",
    "REPOSITORY",
    "domain_arn",
    "domain_policy",
    "package_arn",
    "parse_registry_ref",
    "publish_policy",
    "read_policy",
    "reader_repos",
    "reader_role_patterns",
    "registry_ref_value",
    "repository_arn",
    "repository_policy",
]

OWNER_REPO = "financialplanning"
DOMAIN = "finplan"
REPOSITORY = "contracts"
FORMATS: tuple[str, ...] = ("pypi", "npm")
PYPI_PACKAGE = "finplan-contracts"
NPM_NAMESPACE = "finplan"
NPM_PACKAGE = "contracts"
REGISTRY_REF_PARAMETER = _ssm.build(_ssm.SHARED, OWNER_REPO, "contract", "registry-ref")
CODEARTIFACT_SERVICE = "codeartifact.amazonaws.com"

#: Repository-level read actions (installing a pinned version and resolving the endpoint).
READ_ACTIONS: tuple[str, ...] = ("codeartifact:GetRepositoryEndpoint", "codeartifact:ReadFromRepository")
#: Package-level actions of the publish step (never delete, dispose or update a version status).
PUBLISH_ACTIONS: tuple[str, ...] = (
    "codeartifact:PublishPackageVersion",
    "codeartifact:PutPackageMetadata",
    "codeartifact:DescribePackageVersion",
    "codeartifact:ListPackageVersionAssets",
    "codeartifact:GetPackageVersionAsset",
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,49}\Z")


# ------------------------------------------------------------------ ARNs
def domain_arn(*, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT, domain: str = DOMAIN) -> str:
    return f"arn:{partition}:codeartifact:{region}:{account}:domain/{domain}"


def repository_arn(*, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT, domain: str = DOMAIN, repository: str = REPOSITORY) -> str:
    return f"arn:{partition}:codeartifact:{region}:{account}:repository/{domain}/{repository}"


def package_arn(fmt: str = "*", namespace: str = "", package: str = "*", *, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT, domain: str = DOMAIN, repository: str = REPOSITORY) -> str:
    """``package/<domain>/<repository>/<format>/<namespace>/<package>``; the defaults cover every package."""
    if fmt == "*":
        return f"arn:{partition}:codeartifact:{region}:{account}:package/{domain}/{repository}/*"
    return f"arn:{partition}:codeartifact:{region}:{account}:package/{domain}/{repository}/{fmt}/{namespace}/{package}"


# ------------------------------------------------------------------ reference
def registry_ref_value(region: str, *, domain: str = DOMAIN, repository: str = REPOSITORY) -> dict[str, Any]:
    """The registry reference document (stored as JSON text in :data:`REGISTRY_REF_PARAMETER`)."""
    return {"domain": domain, "repository": repository, "region": region, "formats": list(FORMATS)}


def parse_registry_ref(value: str) -> dict[str, Any]:
    """Parse and check a registry reference value; raises :class:`ValueError`."""
    try:
        doc = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{REGISTRY_REF_PARAMETER} is not JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{REGISTRY_REF_PARAMETER} must be a JSON object")
    problems = [f"{k} must be a lowercase CodeArtifact name" for k in ("domain", "repository") if not isinstance(doc.get(k), str) or not _NAME_RE.match(doc[k])]
    if not isinstance(doc.get("region"), str) or not re.match(r"^[a-z]{2}(-[a-z]+)+-[0-9]\Z", doc["region"]):
        problems.append("region must be an AWS region name")
    formats = doc.get("formats")
    if not isinstance(formats, list) or not set(formats) <= set(FORMATS) or not formats:
        problems.append(f"formats must be a non-empty subset of {list(FORMATS)}")
    if set(doc) - {"domain", "repository", "region", "formats"}:
        problems.append("unknown fields (the reference never holds an account ID, ARN or endpoint)")
    if problems:
        raise ValueError(f"{REGISTRY_REF_PARAMETER}: " + "; ".join(problems))
    return doc


# ------------------------------------------------------------------ identity policies
def _arns(partition: str, region: str, account: str) -> dict[str, str]:
    return {
        "domain": domain_arn(partition=partition, region=region, account=account),
        "repository": repository_arn(partition=partition, region=region, account=account),
        "packages": package_arn(partition=partition, region=region, account=account),
        "reference": ssm_parameter_arn(REGISTRY_REF_PARAMETER, partition=partition, region=region, account=account),
    }


def _read_statements(arns: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {"Sid": "ContractRegistryToken", "Effect": "Allow", "Action": ["codeartifact:GetAuthorizationToken"], "Resource": arns["domain"]},
        {
            "Sid": "ContractRegistryBearerToken",
            "Effect": "Allow",
            "Action": ["sts:GetServiceBearerToken"],
            "Resource": "*",
            "Condition": {"StringEquals": {"sts:AWSServiceName": CODEARTIFACT_SERVICE}},
        },
        {"Sid": "ContractRegistryRead", "Effect": "Allow", "Action": list(READ_ACTIONS), "Resource": arns["repository"]},
        {"Sid": "ContractRegistryReference", "Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": arns["reference"]},
    ]


def read_policy(*, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT) -> PolicyDocument:
    """Identity policy for a consumer build role that installs a pinned contract version."""
    return {"Version": "2012-10-17", "Statement": _read_statements(_arns(partition, region, account))}


def publish_policy(*, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT) -> PolicyDocument:
    """Identity policy for the FinancialPlanning build role: read plus publish, this repository only."""
    arns = _arns(partition, region, account)
    return {
        "Version": "2012-10-17",
        "Statement": [
            *_read_statements(arns),
            {"Sid": "ContractRegistryPublish", "Effect": "Allow", "Action": list(PUBLISH_ACTIONS), "Resource": arns["packages"]},
        ],
    }


# ------------------------------------------------------------------ resource policies
def reader_repos(owner: str = OWNER_REPO) -> list[str]:
    """The repositories whose build roles read the registry (every repository but the owner)."""
    return [r for r in _ssm.REPOS if r != owner]


def reader_role_patterns(repos: Iterable[str] | None = None, *, partition: str = PARTITION, account: str = ACCOUNT) -> list[str]:
    """``arn:<partition>:iam::<account>:role/finplan-shared-<repo>-*`` for every reader repository."""
    return [f"arn:{partition}:iam::{account}:role/finplan-{_ssm.SHARED}-{repo}-*" for repo in (repos if repos is not None else reader_repos())]


def _resource_statement(sid: str, actions: list[str], *, partition: str, account: str, repos: Iterable[str] | None) -> dict[str, Any]:
    return {
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"AWS": "*"},
        "Action": actions,
        "Resource": "*",
        "Condition": {
            "StringEquals": {"aws:PrincipalAccount": account},
            "ArnLike": {"aws:PrincipalArn": reader_role_patterns(repos, partition=partition, account=account)},
        },
    }


def domain_policy(*, partition: str = PARTITION, account: str = ACCOUNT, repos: Iterable[str] | None = None) -> PolicyDocument:
    """Domain resource policy: the other repositories' build roles may get an authorization token."""
    return {"Version": "2012-10-17", "Statement": [_resource_statement("ConsumerBuildRolesToken", ["codeartifact:GetAuthorizationToken"], partition=partition, account=account, repos=repos)]}


def repository_policy(*, partition: str = PARTITION, account: str = ACCOUNT, repos: Iterable[str] | None = None) -> PolicyDocument:
    """Repository resource policy: the other repositories' build roles may read (never publish)."""
    return {"Version": "2012-10-17", "Statement": [_resource_statement("ConsumerBuildRolesRead", list(READ_ACTIONS), partition=partition, account=account, repos=repos)]}
