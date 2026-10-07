"""Deployed beta and gamma suites (tasks 11.1-11.3; PIPE-04, PIPE-05, API-01, API-06..09, ID-03..ID-11, ENV-03).

These tests run IN THE PIPELINE: the beta stage's ``IntegrationTests`` action (``FINPLAN_SUITE=integration-beta``)
and the gamma stage's tests action (``FINPLAN_SUITE=gamma``), started by ``scripts/stage_runner.py``
in the per-environment stage project with the stage role ``finplan-<env>-financialplanning-operator-pipeline-stage``
(an operator principal the plan API resource policy admits). They are skipped everywhere else,
so the offline suite never reaches AWS. The stage fails if none of them executes.

The requests are proven offline first against the deployment double
(``tests/unit/ops/test_integration_double.py``). Every write targets a fresh synthetic portfolio
and plan of this run, with run-unique idempotency keys.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.integration.lifecycle_suite import gamma_isolation_checks, run_key_from, run_lifecycle
from tests.offline_env import FAKE_ACCESS_KEY, OFFLINE_MARKER
from tests.smoke.transport import SigV4Transport, endpoint_parameter

ENV = os.environ.get("FINPLAN_TARGET_ENV")
SUITE = os.environ.get("FINPLAN_SUITE")
SUITES = {"beta": "integration-beta", "gamma": "gamma"}
pytestmark = pytest.mark.skipif(not ENV or SUITES.get(ENV) != SUITE, reason="deployed integration suites run only in the pipeline's beta and gamma stages (FINPLAN_TARGET_ENV, FINPLAN_SUITE)")

OFFLINE_FAKE_KEY = FAKE_ACCESS_KEY


@pytest.fixture(scope="module")
def deployed() -> dict[str, Any]:  # pragma: no cover - needs a deployment
    import boto3
    from finplan_platform.core.config import load_config

    assert ENV is not None
    cfg = load_config(ENV)
    session = boto3.session.Session(region_name=cfg.region)
    endpoint = session.client("ssm").get_parameter(Name=endpoint_parameter(ENV))["Parameter"]["Value"]
    creds = session.get_credentials()
    assert creds is not None, "no AWS credentials in the stage project"
    return {"cfg": cfg, "session": session, "endpoint": endpoint, "transport": SigV4Transport(endpoint, cfg.region, creds.get_frozen_credentials(), correlation_prefix=f"cor_it{ENV}")}


def _parallel(calls: Any) -> list[Any]:  # pragma: no cover - needs a deployment
    calls = list(calls)
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(lambda c: c(), calls))


def test_stage_runs_with_real_credentials_not_the_offline_fakes() -> None:
    """Defect 2 regression: the deployed suites keep the stage role's credentials."""
    assert OFFLINE_MARKER not in os.environ, "the offline-safety environment was applied to a deployed suite"
    assert os.environ.get("AWS_ACCESS_KEY_ID") != OFFLINE_FAKE_KEY
    assert os.environ.get("AWS_CONFIG_FILE") != os.devnull and os.environ.get("AWS_SHARED_CREDENTIALS_FILE") != os.devnull


def test_endpoint_resolves_from_ssm_and_reads_are_contract_envelopes_PIPE_04(deployed: dict[str, Any]) -> None:  # pragma: no cover - needs a deployment
    assert deployed["endpoint"].startswith("https://") and ".execute-api." in deployed["endpoint"]
    code, body, headers = deployed["transport"].call("GET", "/v1/plans/pl_01KDVDNAZ83BAMMYCEGWF33DPM")
    assert code == 404 and body["code"] == "NOT_FOUND", (code, body.get("code"), body.get("message"))
    assert body["correlation_id"].startswith(f"cor_it{ENV}")


def test_synthetic_lifecycle_end_to_end(deployed: dict[str, Any]) -> None:  # pragma: no cover - needs a deployment
    now = datetime.now(UTC)
    run_key = run_key_from(ENV, os.environ.get("FINPLAN_RELEASE_ID"), (os.environ.get("CODEBUILD_BUILD_ID") or "manual").rsplit(":", 1)[-1][:8], now.strftime("%Y%m%d%H%M%S"))
    result = run_lifecycle(deployed["transport"], run_key=run_key, dataset_id=deployed["cfg"].dataset_id, today=now.date(), parallel=_parallel)
    assert result.execution_id.startswith("exe_") and result.checksum.startswith("sha256:")


@pytest.mark.skipif(ENV != "gamma", reason="gamma isolation suite (task 11.3)")
def test_gamma_is_isolated_from_prod_ENV_03(deployed: dict[str, Any]) -> None:  # pragma: no cover - needs a deployment
    denials = gamma_isolation_checks(deployed["session"], env="gamma", other="prod")
    assert len(denials) == 4
