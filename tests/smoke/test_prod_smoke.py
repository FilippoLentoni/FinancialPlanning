"""Smoke suite against a deployed environment (PIPE-05; runs in the pipeline's prod ``SmokeTests`` action).

Phase-aware (decision 26): in phase 2 the lifecycle reuses the latest scheduled real universe snapshot
(no ingestion, no provider call) and a second, read-only test checks that snapshot and the trigger
outcome (task 4.5); plan records stay synthetic.

Skipped unless ``FINPLAN_SUITE=smoke`` and ``FINPLAN_TARGET_ENV`` are set (``scripts/stage_runner.py``
sets both in the stage project). It uses the stage role's credentials and the endpoint published
at ``/finplan/<env>/financialplanning/api/plan-endpoint``. Never part of the unit suite; the local
proof against a deployment double is ``tests/unit/ops/test_smoke_double.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from tests.smoke.smoke_suite import UniversePending, check_universe, run_smoke
from tests.smoke.transport import SigV4Transport, SsmSmokeState, endpoint_parameter

ENV = os.environ.get("FINPLAN_TARGET_ENV")
pytestmark = pytest.mark.skipif(os.environ.get("FINPLAN_SUITE") != "smoke" or not ENV, reason="deployed smoke runs only in the pipeline (FINPLAN_SUITE=smoke, FINPLAN_TARGET_ENV)")


def test_prod_smoke_on_the_synthetic_portfolio() -> None:  # pragma: no cover - needs a deployment
    import boto3
    from finplan_platform.core.config import load_config

    assert ENV is not None
    cfg = load_config(ENV)
    session = boto3.session.Session(region_name=cfg.region)
    ssm = session.client("ssm")
    endpoint = ssm.get_parameter(Name=endpoint_parameter(ENV))["Parameter"]["Value"]
    transport = SigV4Transport(endpoint, cfg.region, session.get_credentials().get_frozen_credentials())
    run_key = os.environ.get("FINPLAN_RELEASE_ID") or datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    result = run_smoke(transport, SsmSmokeState(ssm, ENV), run_key=f"{run_key}-{os.environ.get('PIPELINE_EXECUTION_ID', 'manual')[:8]}", dataset_id=cfg.dataset_id, today=datetime.now(UTC).date(), phase=int(cfg.phase))
    assert result.execution_id.startswith("exe_")


def test_prod_phase2_universe_snapshot_and_trigger_outcome() -> None:  # pragma: no cover - needs a deployment
    """Task 4.5 (add-research-universe-and-daily-loop), read-only: no ingestion and no job submission."""
    import boto3
    from finplan_platform.core.config import load_config

    assert ENV is not None
    cfg = load_config(ENV)
    if cfg.phase != 2:
        pytest.skip(f"{ENV} is phase 1: the universe is served by the fixture provider")
    session = boto3.session.Session(region_name=cfg.region)
    endpoint = session.client("ssm").get_parameter(Name=endpoint_parameter(ENV))["Parameter"]["Value"]
    transport = SigV4Transport(endpoint, cfg.region, session.get_credentials().get_frozen_credentials())
    try:
        found = check_universe(transport, datetime.now(UTC).date())
    except UniversePending as exc:
        pytest.skip(str(exc))
    assert found["outcome"] in ("skipped_no_strategy", "pending_approval")
