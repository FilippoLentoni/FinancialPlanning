"""Smoke suite against a deployed environment (PIPE-05; runs in the pipeline's prod ``SmokeTests`` action).

Skipped unless ``FINPLAN_SUITE=smoke`` and ``FINPLAN_TARGET_ENV`` are set (``scripts/stage_runner.py``
sets both in the stage project). It uses the stage role's credentials and the endpoint published
at ``/finplan/<env>/financialplanning/api/plan-endpoint``. Never part of the unit suite; the local
proof against a deployment double is ``tests/unit/ops/test_smoke_double.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from tests.smoke.smoke_suite import run_smoke
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
    result = run_smoke(transport, SsmSmokeState(ssm, ENV), run_key=f"{run_key}-{os.environ.get('PIPELINE_EXECUTION_ID', 'manual')[:8]}", dataset_id=cfg.dataset_id, today=datetime.now(UTC).date())
    assert result.execution_id.startswith("exe_")
