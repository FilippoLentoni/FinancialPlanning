"""Sweep Lambda adapter wiring (tasks 2.4, 3.4): the handler runs both sweeps through injected clients."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from finplan_platform.core.artifacts import ArtifactStore, key_plan_content
from finplan_platform.core.clock import FrozenClock
from finplan_platform.handlers.sweep import run
from tests.fakes import FakeDynamoDB


def test_run_reports_counts_without_keys(s3: Any, buckets: dict[str, str], ddb: FakeDynamoDB) -> None:
    store = ArtifactStore(s3, buckets)
    store.put_once("plans", key_plan_content("pl_01KDVDNAZ83BAMMYCEGWF33DPM", "pv_01KDVDNAZ83BAMMYCEGWF33DPM"), b"{}")
    clock = FrozenClock(datetime.now(UTC))
    clock.advance(hours=30)
    out = run(env="beta", s3=s3, dynamodb=ddb, buckets={"plans": buckets["plans"], "snapshots": buckets["snapshots"]}, grace_hours=24, retention_days=14, clock=clock)
    assert out["orphans"]["deleted"] == 1 and out["snapshots"]["expired_snapshots"] == []
    assert "pl_01" not in str(out["orphans"])  # counts only; keys stay in the platform
    assert out["correlation_id"].startswith("cor_")
