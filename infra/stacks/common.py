"""Shared CDK building blocks for every platform stack (FOUNDATION-owned).

Use these helpers in every stack so naming, tags, permission boundaries and code
packaging stay uniform:

* :class:`PlatformStack`: base class. Applies the contract cost-allocation tags
  (``project``, ``owner-repo``, ``environment``) to everything in the stack and the
  environment permission boundary (``finplan-<env>-permission-boundary``, created by the
  bootstrap/tooling stack) to **every** IAM role in the stack, CDK-generated ones included.
  Each resource still needs its own ``logical-role`` tag: call :func:`tag_role`.
* :func:`resource_name` / :func:`platform_role_name`: ``finplan-<env>-financialplanning-<logical>``
  names (the contract naming convention the boundaries' name-based denies rely on).
* :func:`platform_role`: an explicitly named role; pass it to ``lambda_.Function(role=...)``
  so platform principals match the bucket/table policies' ``finplan-<env>-financialplanning-*``
  pattern.
* :func:`role_arn_pattern`: ``arn:${Partition}:iam::${AccountId}:role/<pattern>`` built
  from tokens (no account literal ever appears in a file).
* :func:`lambda_code`: the function code asset. The build stage builds one bundle per function
  (:mod:`scripts.lambda_bundle`: platform package + the locked dependency closure for arm64 +
  config) and sets ``FINPLAN_LAMBDA_BUNDLE_DIR`` (``<dir>/<function>``; ``FINPLAN_LAMBDA_BUNDLE``
  is still accepted as one bundle for every function). In **release mode**
  (``FINPLAN_RELEASE_BUILD=1``, or any CodeBuild build unless ``FINPLAN_RELEASE_BUILD=0``) a missing
  or incomplete bundle FAILS the synth: a source-only package is never deployable (it shipped
  once and every function failed at init with ``No module named 'finplan_contracts'``, see
  docs/pipeline.md). Outside release mode (local and offline synth, tests) the source tree is used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import aws_cdk as cdk
from aws_cdk import Aws, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct, IConstruct
from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts import ssm as contract_ssm

from finplan_platform.core.config import EnvConfig

__all__ = [
    "REPO",
    "REPO_ROOT",
    "PlatformStack",
    "StageContext",
    "resource_name",
    "platform_role_name",
    "platform_role",
    "role_arn_pattern",
    "platform_principal_pattern",
    "tag_role",
    "lambda_code",
    "release_mode",
    "bundle_path",
    "bundle_problems",
    "SourceOnlyCodeError",
    "BUNDLE_DIR_ENV",
    "RELEASE_ENV",
    "ssm_name",
    "PLATFORM_ROLES",
]

REPO = "financialplanning"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Logical names of the platform's own runtime roles (role name = finplan-<env>-financialplanning-<logical>-role).
#: Owners: plan-api-handler (API agent), ingestion-handler (INGEST agent), metadata-sweeper (FOUNDATION).
PLATFORM_ROLES = {
    "plan_api": "plan-api-handler",
    "ingestion": "ingestion-handler",
    "sweeper": "metadata-sweeper",
    "schedule": "daily-ingest-schedule",
}


def resource_name(env: str, logical: str, suffix: str | None = None) -> str:
    return contract_boundaries.resource_name(env, REPO, logical, suffix)


def platform_role_name(env: str, logical: str) -> str:
    name = resource_name(env, logical, "role")
    if len(name) > 64:
        raise ValueError(f"role name {name!r} exceeds 64 characters")
    return name


def role_arn_pattern(role_name_pattern: str) -> str:
    """Role ARN pattern built from CloudFormation pseudo parameters (no account literal)."""
    partition, account = Aws.PARTITION, Aws.ACCOUNT_ID
    return f"arn:{partition}:iam::{account}:role/{role_name_pattern}"


def platform_principal_pattern(env: str) -> str:
    """Every platform role of ``env``: ``finplan-<env>-financialplanning-*``."""
    return role_arn_pattern(f"finplan-{env}-{REPO}-*")


def ssm_name(env: str, category: str, name: str) -> str:
    return contract_ssm.build(env, REPO, category, name)


def tag_role(construct: IConstruct, logical_role: str) -> None:
    """Set the ``logical-role`` cost-allocation tag (ownership-matrix key) on a construct tree."""
    Tags.of(construct).add("logical-role", logical_role)


class PlatformStack(Stack):
    """Base stack: contract tags, environment permission boundary, termination protection in prod."""

    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, description: str, **kwargs: Any) -> None:
        super().__init__(
            scope,
            construct_id,
            env=cdk.Environment(region=cfg.region),
            description=description,
            termination_protection=cfg.env == "prod",
            **kwargs,
        )
        self.cfg = cfg
        self.env_name = cfg.env
        base = contract_ssm.cost_allocation_tags(REPO, cfg.env, "placeholder")
        for key in ("project", "owner-repo", "environment"):
            Tags.of(self).add(key, base[key])
        boundary = iam.ManagedPolicy.from_managed_policy_name(self, "EnvPermissionBoundary", contract_boundaries.boundary_name(cfg.env))
        iam.PermissionsBoundary.of(self).apply(boundary)


def platform_role(scope: Construct, construct_id: str, *, cfg: EnvConfig, logical: str, assumed_by: iam.IPrincipal, description: str) -> iam.Role:
    role = iam.Role(scope, construct_id, role_name=platform_role_name(cfg.env, logical), assumed_by=assumed_by, description=description)
    tag_role(role, logical)
    return role


#: Environment variables that select the Lambda code (see :func:`lambda_code`).
BUNDLE_DIR_ENV = "FINPLAN_LAMBDA_BUNDLE_DIR"
BUNDLE_ENV = "FINPLAN_LAMBDA_BUNDLE"
RELEASE_ENV = "FINPLAN_RELEASE_BUILD"
#: Marker file of a build-stage bundle (``scripts/lambda_bundle.py``).
BUNDLE_MANIFEST = "bundle-manifest.json"
#: Entries a deployable bundle must contain besides the platform package.
BUNDLE_REQUIRED = ("finplan_platform", "finplan_contracts", "jsonschema", "rfc8785", "config", BUNDLE_MANIFEST)


class SourceOnlyCodeError(RuntimeError):
    """Release-mode synth found no complete dependency bundle for a function."""


def release_mode(environ: Mapping[str, str] | None = None) -> bool:
    """True when the synth produces release assets: ``FINPLAN_RELEASE_BUILD=1``, or a CodeBuild
    build (``CODEBUILD_BUILD_ID``) unless ``FINPLAN_RELEASE_BUILD=0`` (the offline tests set it)."""
    env = os.environ if environ is None else environ
    flag = env.get(RELEASE_ENV, "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    return bool(env.get("CODEBUILD_BUILD_ID"))


def bundle_path(function: str, environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    if env.get(BUNDLE_DIR_ENV):
        return Path(env[BUNDLE_DIR_ENV]) / function
    if env.get(BUNDLE_ENV):
        return Path(env[BUNDLE_ENV])
    return None


def bundle_problems(path: Path) -> list[str]:
    if not path.is_dir():
        return [f"{path} does not exist"]
    return [f"{name} missing" for name in BUNDLE_REQUIRED if not ((path / name).exists() or (path / f"{name}.py").is_file())]


def lambda_code(function: str, *, environ: Mapping[str, str] | None = None) -> lambda_.Code:
    """Code asset of the platform function ``function`` (``plan-api``, ``ingestion``, ``sweeper``)."""
    path = bundle_path(function, environ)
    release = release_mode(environ)
    if path is not None:
        problems = bundle_problems(path)
        if problems and release:
            raise SourceOnlyCodeError(f"Lambda bundle for {function} at {path} is incomplete: {'; '.join(problems)}")
        if not problems:
            return lambda_.Code.from_asset(str(path))
    if release:
        raise SourceOnlyCodeError(
            f"release synth without a dependency bundle for the {function} function: a source-only package fails at init "
            f"(No module named 'finplan_contracts'). Build the bundles (scripts/lambda_bundle.py) and set {BUNDLE_DIR_ENV}."
        )
    return lambda_.Code.from_asset(
        str(REPO_ROOT / "platform"),
        exclude=["**/__pycache__", "**/*.pyc", "**/.pytest_cache"],
    )


@dataclass
class StageContext:
    """What optional stack modules receive from ``infra/app.py`` (see ``add_to_stage``)."""

    cfg: EnvConfig
    shared: Mapping[str, Any]
    storage: Any = None  # infra.stacks.storage.StorageStack
    metadata: Any = None  # infra.stacks.metadata.MetadataStack
    extras: dict[str, Any] = field(default_factory=dict)
