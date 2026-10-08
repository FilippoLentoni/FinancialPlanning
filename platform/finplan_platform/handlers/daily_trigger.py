"""Step Lambda of the daily recommendation trigger state machine (daily-recommendation-trigger).

Event shapes (from the state machine's Task states)::

    {"step": "start", "event": <EventBridge event or manual start input>}  -> initial state
    {"step": "<check_snapshot|read_strategy|...>", "state": {...}}          -> next state

Wiring (environment): ``FINPLAN_ENV``, ``FINPLAN_BUCKET_REPORTS``, ``FINPLAN_KMS_KEY_ARN``,
``FINPLAN_CONFIG_DIR``. The FinanceModel job endpoint and the plan endpoint are resolved from their
registered SSM keys at run time; both calls are IAM-signed (SigV4, ``execute-api``) with the step
function's role, which may call only ``submit_job``/``get_job_status`` and the platform routes granted
to the ``automation`` class (never publish).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from finplan_contracts import ssm as contract_ssm

from ..core.daily_trigger import TriggerDeps, run_step, start_input

__all__ = ["SigV4Json", "handler"]

_DEPS: TriggerDeps | None = None
#: the daily job's budget category and worst-case estimate (decision 18: at most about USD 0.12)
DAILY_JOB_CATEGORY = "cpu_research"
DAILY_JOB_ESTIMATE_USD = 0.12


class SigV4Json:
    """Minimal IAM-signed JSON client for an API Gateway endpoint (regional SigV4, service execute-api)."""

    def __init__(self, endpoint: str, region: str, credentials: Any, timeout: float = 25.0) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("endpoint must be an https URL")
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.credentials = credentials
        self.timeout = timeout

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        base = self.endpoint.removesuffix("/v1")
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        req = AWSRequest(method=method, url=base + path, data=data, headers={"Content-Type": "application/json"})
        SigV4Auth(self.credentials.get_frozen_credentials(), "execute-api", self.region).add_auth(req)
        http = urllib.request.Request(req.url, data=data, headers=dict(req.headers.items()), method=method)
        try:
            with urllib.request.urlopen(http, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                return exc.code, {}


class _JobApi:
    def __init__(self, client: SigV4Json) -> None:
        self.client = client

    def submit_job(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.client.call("POST", "/v1/jobs", dict(body))

    def get_job_status(self, run_id: str) -> tuple[int, dict[str, Any]]:
        return self.client.call("GET", f"/v1/jobs/{run_id}")


class _PlanApi:
    def __init__(self, client: SigV4Json) -> None:
        self.client = client

    def get_snapshot(self, input_snapshot_id: str) -> tuple[int, dict[str, Any]]:
        return self.client.call("GET", f"/v1/snapshots/{input_snapshot_id}")

    def get_plan(self, plan_id: str) -> tuple[int, dict[str, Any]]:
        return self.client.call("GET", f"/v1/plans/{plan_id}")

    def accept(self, plan_id: str, run_id: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.client.call("POST", f"/v1/plans/{plan_id}/staged-outputs/{run_id}/accept", dict(body))


def deps_from_environment(environ: Mapping[str, str] | None = None) -> TriggerDeps:  # pragma: no cover - Lambda wiring
    import boto3
    from finplan_contracts.budget import STATE_PARAMETER, preflight

    from ..core.artifacts import ArtifactStore
    from ..core.aws_clients import s3_client
    from ..core.config import load_config

    e = dict(environ or os.environ)
    cfg = load_config(e["FINPLAN_ENV"])
    session = boto3.session.Session()
    region = session.region_name or cfg.region
    ssm = session.client("ssm", region_name=region)

    def param(name: str) -> str:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]

    creds = session.get_credentials()
    job = SigV4Json(param(contract_ssm.build(cfg.env, "financemodel", "api", "job-endpoint")), region, creds)
    plan = SigV4Json(param(contract_ssm.build(cfg.env, "financialplanning", "api", "plan-endpoint")), region, creds)
    store = ArtifactStore(s3_client(region), {"reports": e["FINPLAN_BUCKET_REPORTS"]}, kms_key_id=e.get("FINPLAN_KMS_KEY_ARN") or None)

    def budget_denied() -> bool:
        try:
            state = param(STATE_PARAMETER)
        except ssm.exceptions.ParameterNotFound:
            state = None
        return not preflight(DAILY_JOB_CATEGORY, DAILY_JOB_ESTIMATE_USD, budget_state=state).allowed

    return TriggerDeps(
        config=cfg,
        ssm=ssm,
        job_api=_JobApi(job),
        plan_api=_PlanApi(plan),
        write_outcome=lambda key, body: store.put_once("reports", key, body, "application/json"),
        budget_denied=budget_denied,
    )


def handler(event: Mapping[str, Any], context: Any = None, *, deps: TriggerDeps | None = None) -> dict[str, Any]:
    global _DEPS
    if deps is None:
        if _DEPS is None:
            _DEPS = deps_from_environment()
        deps = _DEPS
    step = str(event.get("step") or "")
    if step == "start":
        return start_input(event.get("event") or {}, deps.config.env)
    return run_step(step, dict(event.get("state") or {}), deps)
