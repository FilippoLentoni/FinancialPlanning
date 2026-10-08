"""Daily sweep Lambda adapter (tasks 2.4, 3.4): thin wiring onto :mod:`finplan_platform.core.sweeps`.

Environment: ``FINPLAN_ENV``, ``FINPLAN_BUCKET_PLANS``, ``FINPLAN_BUCKET_SNAPSHOTS``,
``FINPLAN_ORPHAN_GRACE_HOURS`` (>= 24) and ``FINPLAN_SNAPSHOT_RETENTION_DAYS`` (empty = no
expiry, prod). Clients are injectable for tests (:func:`run`).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Any, Mapping

from ..core.artifacts import ArtifactStore
from ..core.clock import Clock, SystemClock
from ..core.context import Caller, OperationContext, new_correlation_id
from ..core.repository import MetadataRepository
from ..core.sweeps import expired_snapshot_sweep, orphan_sweep

log = logging.getLogger(__name__)

__all__ = ["handler", "run"]


def run(*, env: str, s3: Any, dynamodb: Any, buckets: Mapping[str, str], grace_hours: int, retention_days: int | None, clock: Clock | None = None, principal: str = "metadata-sweeper") -> dict[str, Any]:
    ctx = OperationContext(caller=Caller(principal=principal, channel="scheduler"), env=env, correlation_id=new_correlation_id(), clock=clock or SystemClock(), trigger="scheduled")
    repo = MetadataRepository(dynamodb, env)
    store = ArtifactStore(s3, buckets, clock=ctx.clock)
    orphans = orphan_sweep(ctx, repo, store, grace=timedelta(hours=grace_hours))
    expired = expired_snapshot_sweep(ctx, repo, store, retention_days=retention_days)
    return {"correlation_id": ctx.correlation_id, "orphans": orphans.to_dict(), "snapshots": expired.to_dict()}


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover - AWS entry point
    import boto3

    from ..core.aws_clients import s3_client

    env = os.environ["FINPLAN_ENV"]
    retention = os.environ.get("FINPLAN_SNAPSHOT_RETENTION_DAYS") or None
    result = run(
        env=env,
        s3=s3_client(),
        dynamodb=boto3.client("dynamodb"),
        buckets={"plans": os.environ["FINPLAN_BUCKET_PLANS"], "snapshots": os.environ["FINPLAN_BUCKET_SNAPSHOTS"]},
        grace_hours=int(os.environ.get("FINPLAN_ORPHAN_GRACE_HOURS", "24")),
        retention_days=int(retention) if retention else None,
        principal=getattr(context, "invoked_function_arn", None) or "metadata-sweeper",
    )
    log.info(json.dumps(result))
    return result
