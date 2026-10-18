"""Deployed beta modeling competition functions (infra/stacks/modeling.py): read-only preflights.

Runs IN THE PIPELINE only (beta ``integration-beta`` suite, ``scripts/stage_runner.py``, stage role
``finplan-beta-financialplanning-operator-pipeline-stage``, admitted by each function's resource
policy) and is skipped everywhere else. The preflight imports the bundled CLIs, resolves the
evidence backend from the function's deploy-time environment and reads the experiment registry
and the view prefix; it writes nothing and calls no market source.
"""

from __future__ import annotations

import json
import os

import pytest

ENV = os.environ.get("FINPLAN_TARGET_ENV")
SUITE = os.environ.get("FINPLAN_SUITE")
pytestmark = pytest.mark.skipif(ENV != "beta" or SUITE != "integration-beta", reason="deployed modeling preflights run only in the pipeline's beta stage")


@pytest.mark.parametrize("function,event", [("modeling-report", {"mode": "preflight"}), ("modeling-sources", {"action": "preflight"})])
def test_deployed_modeling_function_preflight(function: str, event: dict) -> None:  # pragma: no cover - needs a deployment
    import boto3
    from botocore.config import Config

    client = boto3.client("lambda", region_name="us-east-2", config=Config(read_timeout=300, retries={"max_attempts": 2}))
    resp = client.invoke(FunctionName=f"finplan-beta-financialplanning-{function}", Payload=json.dumps(event).encode())
    payload = json.loads(resp["Payload"].read() or b"null")
    assert resp["StatusCode"] == 200 and "FunctionError" not in resp, payload
    assert payload["status"] == "ok" and payload["action"] == "preflight" and payload["function"] == function, payload
    assert payload["experiment"] == "beta-competition-v1"
    if function == "modeling-report":
        assert "views_present" in payload
