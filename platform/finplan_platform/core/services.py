"""Injected dependencies of the plan lifecycle operations (API-owned; tasks 4.x, 5.x).

Every operation in :mod:`~finplan_platform.core.plans`, ``versions``, ``validation``,
``publication``, ``execution`` and ``snapshot_reads`` takes ``(ctx, svc, ...)``:

* ``ctx``: :class:`~finplan_platform.core.context.OperationContext` (caller, env, clock, IDs);
* ``svc``: :class:`Services`, the per-environment clients and configuration. It also
  satisfies the ``deps`` protocol of :mod:`~finplan_platform.core.snapshots` (``config``,
  ``repo``, ``store``), so the API passes it straight through to those reads.

Handlers build one :class:`Services` per Lambda container (:meth:`Services.from_lambda_environment`);
tests build it from the in-memory DynamoDB fake, moto S3 and ``config/<env>.json``.
Nothing here reads a bucket, table or key name from a literal: buckets come from the
``FINPLAN_BUCKET_<ROLE>`` variables the API stack sets from the storage stack, the key ARN
from ``FINPLAN_KMS_KEY_ARN``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .artifacts import ArtifactStore
from .config import BUCKET_ROLES, EnvConfig, load_config
from .errors import PlatformError
from .repository import MetadataRepository

__all__ = ["Services"]


@dataclass
class Services:
    cfg: EnvConfig
    repo: MetadataRepository
    artifacts: ArtifactStore | None = None
    #: optional extra clients (``ssm``, ``lambda``) for delegated modules
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def env(self) -> str:
        return self.cfg.env

    @property
    def config(self) -> EnvConfig:
        """Alias of ``cfg`` (the ingestion/snapshot modules' ``deps`` protocol: ``config``, ``repo``, ``store``)."""
        return self.cfg

    @property
    def store(self) -> ArtifactStore:
        if self.artifacts is None:
            raise PlatformError("DEPENDENCY_UNAVAILABLE", "artifact storage is not configured in this environment", retryable=False)
        return self.artifacts

    @classmethod
    def from_lambda_environment(cls, environ: dict[str, str] | None = None) -> Services:  # pragma: no cover - AWS entry point
        """Build from the Lambda environment (``FINPLAN_ENV``, ``FINPLAN_BUCKET_*``, ``FINPLAN_KMS_KEY_ARN``)."""
        import boto3

        env_vars = dict(environ if environ is not None else os.environ)
        env = env_vars["FINPLAN_ENV"]
        cfg = load_config(env)
        buckets = {r: env_vars[f"FINPLAN_BUCKET_{r.upper()}"] for r in BUCKET_ROLES if env_vars.get(f"FINPLAN_BUCKET_{r.upper()}")}
        repo = MetadataRepository(boto3.client("dynamodb"), env, idempotency_ttl_days=int(cfg.metadata["idempotency_ttl_days"]))
        store = ArtifactStore(boto3.client("s3"), buckets, kms_key_id=env_vars.get("FINPLAN_KMS_KEY_ARN") or None) if buckets else None
        return cls(cfg=cfg, repo=repo, artifacts=store, extras={"ssm": boto3.client("ssm")})
