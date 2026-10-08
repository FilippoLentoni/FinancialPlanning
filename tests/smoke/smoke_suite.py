"""Prod smoke suite on the synthetic smoke portfolio (task 10.4; PIPE-05; contracts D6 "Smoke tests on a
synthetic portfolio").

Spec platform-pipeline "Environment tests per stage", scenario "Prod smoke": smoke creates,
validates and publishes a version and records a paper execution, all on the synthetic smoke
portfolio, and verifies read-back checksums. It never mutates any other plan, never asserts
changing market values and never calls a real market-data provider:

* **phase 1**: the input snapshot comes from an on-demand fixture ingestion and :func:`run_smoke`
  refuses a snapshot whose lineage is not synthetic;
* **phase 2** (real ``yfinance`` data, decision 26): smoke starts NO ingestion. It reuses the
  universe snapshot of the latest scheduled run, found through the trigger-outcome record
  (``GET /v1/daily-trigger/outcomes/<session>``); the plan records stay synthetic. The separate,
  read-only :func:`check_universe` (task 4.5 of add-research-universe-and-daily-loop) verifies that
  this snapshot is real, approved under ``approval-v2-universe`` with both disclosures, and that the
  trigger outcome is ``skipped_no_strategy`` or ``pending_approval``; it never submits a job.

The suite talks to the plan API through a :class:`Transport` only, so the same code runs

* in prod, through :class:`tests.smoke.transport.SigV4Transport` (IAM-signed HTTPS to the endpoint
  in ``/finplan/<env>/financialplanning/api/plan-endpoint``) - the ``SmokeTests`` pipeline action;
* locally, against a **deployment double**: the in-process API router with the DynamoDB fake, moto
  S3 and the fixture provider (``tests/unit/ops/test_smoke_double.py``).

The dedicated smoke portfolio is created once (``synthetic: true``) and its ID is kept in
:class:`SmokeState` (deployed: ``/finplan/<env>/financialplanning/config/smoke-portfolio-id``,
written by the stage role as the pipeline writer of its own segment). Each run adds a plan to that
portfolio, so runs never interfere with each other or with real plans.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from finplan_contracts.canonical import canonicalize

__all__ = [
    "SMOKE_CONTENT",
    "SMOKE_PORTFOLIO_NAME",
    "UNIVERSE_SMOKE_CONTENT",
    "MemoryState",
    "SmokeError",
    "SmokeResult",
    "SmokeState",
    "Transport",
    "UniversePending",
    "check_universe",
    "content_checksum",
    "latest_universe_outcome",
    "run_smoke",
]

SMOKE_PORTFOLIO_NAME = "finplan smoke portfolio (synthetic)"
#: Synthetic plan content (S&P 500 tracking ETF plus cash); no market value is asserted.
SMOKE_CONTENT: dict[str, Any] = {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.5}], "cash_weight": 0.5},
    "constraints": {"long_only": True, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5},
}
#: Phase 2 plan content on the research-universe snapshot (VOO plus cash); synthetic, no value asserted.
UNIVERSE_SMOKE_CONTENT: dict[str, Any] = {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": "VOO", "weight": 0.5}], "cash_weight": 0.5},
    "constraints": {"long_only": True, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5},
}
#: Trigger outcomes a healthy phase 2 environment may show (no strategy yet, or awaiting the user).
TRIGGER_OUTCOMES_OK = ("skipped_no_strategy", "pending_approval")
DISCLOSURE_KINDS = {"hindsight_selection", "survivorship"}
UNIVERSE_HISTORY_START = "2010-10-01"
OUTCOME_LOOKBACK_DAYS = 10
#: Synthetic lineage for the smoke root version (phase 1 has no FinanceModel release; these are
#: well-formed foreign identifiers, never minted by the platform).
SMOKE_CONFIGURATION_ID = "cfg_" + "0" * 64
SMOKE_MODEL_VERSION = "mv_01KDVDNAZ83BAMMYCEGWF33DPM"
SMOKE_RUN_ID = "run_01KDVDNAZ83BAMMYCEGWF33DPM"


class SmokeError(AssertionError):
    pass


class UniversePending(SmokeError):
    """Phase 2 was just enabled: the latest scheduled universe snapshot is still a phase 1 fixture one."""


class Transport(Protocol):
    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any], dict[str, str]]: ...

    def download(self, url: str) -> bytes: ...


class SmokeState(Protocol):
    def get(self) -> str | None: ...

    def put(self, portfolio_id: str) -> None: ...


class MemoryState:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    def get(self) -> str | None:
        return self.value

    def put(self, portfolio_id: str) -> None:
        self.value = portfolio_id


@dataclass
class SmokeResult:
    portfolio_id: str = ""
    plan_id: str = ""
    input_snapshot_id: str = ""
    plan_version_id: str = ""
    checksum: str = ""
    publication_id: str = ""
    execution_id: str = ""
    steps: list[str] = field(default_factory=list)


def content_checksum(content: Any) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(content)).hexdigest()


def _expect(resp: tuple[int, dict[str, Any], dict[str, str]], status: tuple[int, ...], what: str) -> dict[str, Any]:
    code, body, _ = resp
    if code not in status:
        raise SmokeError(f"{what}: HTTP {code}: {body.get('code')} {body.get('message')}")
    return body


def run_smoke(
    t: Transport,
    state: SmokeState,
    *,
    run_key: str,
    dataset_id: str,
    today: date,
    log: Callable[[str], None] = print,
    phase: int = 1,
) -> SmokeResult:
    """One smoke run (see module docstring). ``run_key`` makes every idempotency key of the run unique."""
    r = SmokeResult()

    def step(text: str) -> None:
        r.steps.append(text)
        log(f"smoke: {text}")

    # 1. the dedicated synthetic portfolio (created once, then reused)
    pf_id = state.get()
    if pf_id:
        pf = _expect(t.call("GET", f"/v1/portfolios/{pf_id}"), (200,), "read smoke portfolio")
        pf = pf.get("portfolio", pf)
    else:
        pf = _expect(t.call("POST", "/v1/portfolios", {"name": SMOKE_PORTFOLIO_NAME, "base_currency": "USD", "synthetic": True, "idempotency_key": "smoke-portfolio-v1"}), (200, 201), "create smoke portfolio")
        state.put(pf["portfolio_id"])
    if pf.get("synthetic") is not True:
        raise SmokeError("the smoke portfolio is not synthetic; smoke never touches a real portfolio")
    r.portfolio_id = pf["portfolio_id"]
    step(f"portfolio {r.portfolio_id} (synthetic)")

    # 2. a plan for this run, on the smoke portfolio only
    plan = _expect(t.call("POST", f"/v1/portfolios/{r.portfolio_id}/plans", {"name": f"smoke {run_key}", "expected_revision": pf["revision"], "idempotency_key": f"smoke-plan-{run_key}"}), (200, 201), "create plan")
    r.plan_id = plan["plan_id"]
    step(f"plan {r.plan_id}")

    # 3. the input snapshot
    content = SMOKE_CONTENT
    if phase == 1:  # on-demand ingestion through the fixture provider
        start = (today - timedelta(days=10)).isoformat()
        end = (today - timedelta(days=1)).isoformat()
        ing = _expect(t.call("POST", "/v1/ingestions", {"dataset_id": dataset_id, "start_date": start, "end_date": end, "granularity": "daily", "idempotency_key": f"smoke-ingest-{run_key}"}), (200, 201), "on-demand ingestion")
        sid = ing.get("input_snapshot_id")
        if not sid:
            raise SmokeError(f"ingestion returned no snapshot for {start}..{end} (quality flags {ing.get('quality_flags')})")
    else:  # phase 2: no ingestion (no real-provider call); the latest scheduled universe snapshot
        rec = latest_universe_outcome(t, today)
        if rec is None:
            raise SmokeError(f"no scheduled universe snapshot recorded in the last {OUTCOME_LOOKBACK_DAYS} days (daily-trigger outcomes)")
        sid, content = str(rec["input_snapshot_id"]), UNIVERSE_SMOKE_CONTENT
    snap = _expect(t.call("GET", f"/v1/snapshots/{sid}"), (200,), "read snapshot")
    snap_doc = snap.get("snapshot", snap)
    if phase == 1 and snap_doc.get("synthetic") is not True:
        raise SmokeError("phase 1 smoke requires a synthetic (fixture) snapshot; a real provider must never run in smoke")
    r.input_snapshot_id = sid
    step(f"snapshot {sid} ({snap_doc.get('status')})")

    # 4. root version, then checksum-bearing read-back
    head = _expect(t.call("GET", f"/v1/plans/{r.plan_id}"), (200,), "read plan head")["plan"]
    body = {
        "expected_revision": head["head"]["revision"],
        "idempotency_key": f"smoke-version-{run_key}",
        "domain": "finance",
        "domain_schema_version": "1.0",
        "content": copy.deepcopy(content),
        "input_snapshot_id": sid,
        "configuration_id": SMOKE_CONFIGURATION_ID,
        "model_version": SMOKE_MODEL_VERSION,
        "run_id": SMOKE_RUN_ID,
    }
    version = _expect(t.call("POST", f"/v1/plans/{r.plan_id}/versions", body), (200, 201), "create version")
    expected = content_checksum(content)
    if version.get("checksum") != expected:
        raise SmokeError(f"version checksum {version.get('checksum')} != JCS SHA-256 of the submitted content {expected}")
    r.plan_version_id, r.checksum = version["plan_version_id"], expected
    got = _expect(t.call("GET", f"/v1/plan-versions/{r.plan_version_id}?download=true"), (200,), "read version")
    if got["plan_version"]["checksum"] != expected or got["content_ref"]["checksum"] != expected:
        raise SmokeError("read-back checksum differs from the created version")
    data = t.download(got["download_grant"]["url"])
    if "sha256:" + hashlib.sha256(data).hexdigest() != expected:
        raise SmokeError("downloaded content does not hash to the version checksum")
    step(f"version {r.plan_version_id} read back, checksum {expected}")

    # 5. validate
    val = _expect(t.call("POST", f"/v1/plan-versions/{r.plan_version_id}/validate", {"idempotency_key": f"smoke-validate-{run_key}"}), (200,), "validate")
    if val.get("status") != "validated":
        raise SmokeError(f"validation result {val.get('status')}: {val.get('findings')}")
    step("validated")

    # 6. publish the exact validated version
    head = _expect(t.call("GET", f"/v1/plans/{r.plan_id}"), (200,), "read plan head")["plan"]
    pub = _expect(
        t.call("POST", f"/v1/plans/{r.plan_id}/publications", {"plan_version_id": r.plan_version_id, "expected_revision": head["publication_revision"], "idempotency_key": f"smoke-publish-{run_key}"}),
        (200, 201),
        "publish",
    )
    if pub.get("plan_version_checksum") != expected:
        raise SmokeError("publication does not record the version checksum")
    r.publication_id = pub["publication_id"]
    before = _expect(t.call("GET", f"/v1/publications/{r.publication_id}"), (200,), "read publication")
    step(f"published {r.publication_id}")

    # 7. paper execution, recorded separately; publication and version untouched
    exe = _expect(t.call("POST", f"/v1/publications/{r.publication_id}/executions", {"mode": "paper", "idempotency_key": f"smoke-execution-{run_key}"}), (200, 201), "record paper execution")
    if exe.get("mode") != "paper" or exe.get("publication_id") != r.publication_id:
        raise SmokeError("execution record does not reference the publication in paper mode")
    r.execution_id = exe["execution_id"]
    _expect(t.call("GET", f"/v1/executions/{r.execution_id}"), (200,), "read execution")
    after = _expect(t.call("GET", f"/v1/publications/{r.publication_id}"), (200,), "read publication")
    if after != before:
        raise SmokeError("recording an execution changed the publication")
    final = _expect(t.call("GET", f"/v1/plan-versions/{r.plan_version_id}"), (200,), "read version")
    if final["plan_version"]["checksum"] != expected:
        raise SmokeError("recording an execution changed the version")
    step(f"paper execution {r.execution_id}; publication and version unchanged")
    return r


# ------------------------------------------------------------------ phase 2 (read-only)
def latest_universe_outcome(t: Transport, today: date, *, lookback_days: int = OUTCOME_LOOKBACK_DAYS) -> dict[str, Any] | None:
    """The most recent trigger-outcome record that names a universe snapshot (GETs only)."""
    for back in range(lookback_days + 1):
        day = (today - timedelta(days=back)).isoformat()
        body = _expect(t.call("GET", f"/v1/daily-trigger/outcomes/{day}"), (200,), f"read trigger outcomes {day}")
        named = [o for o in body.get("outcomes") or [] if o.get("input_snapshot_id")]
        if named:
            return max(named, key=lambda o: str(o.get("recorded_at", "")))
    return None


def check_universe(t: Transport, today: date, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Task 4.5 prod smoke, non-mutating: the latest scheduled universe snapshot is real, complete and
    approved with both disclosures, and the latest trigger outcome is one of :data:`TRIGGER_OUTCOMES_OK`.

    Raises :class:`UniversePending` while the latest scheduled snapshot is still a phase 1 fixture one
    (phase 2 enabled after that day's 09:00 ET run), and :class:`SmokeError` for every real defect.
    """
    rec = latest_universe_outcome(t, today)
    if rec is None:
        raise SmokeError(f"no scheduled universe snapshot recorded in the last {OUTCOME_LOOKBACK_DAYS} days")
    sid = str(rec["input_snapshot_id"])
    snap = _expect(t.call("GET", f"/v1/snapshots/{sid}"), (200,), "read universe snapshot")
    doc = snap.get("snapshot", snap)
    if doc.get("synthetic") is True:
        raise UniversePending(f"the latest scheduled universe snapshot {sid} ({rec.get('session_date')}) is a phase 1 fixture snapshot; the first phase 2 run is the next 09:00 ET schedule")
    problems = []
    if rec.get("outcome") not in TRIGGER_OUTCOMES_OK:
        problems.append(f"trigger outcome {rec.get('outcome')!r} not in {TRIGGER_OUTCOMES_OK}")
    if rec.get("published") is not False:
        problems.append("the trigger outcome reports a publication (only the user publishes)")
    if (doc.get("lineage") or {}).get("provider") != "yfinance":
        problems.append(f"lineage provider {(doc.get('lineage') or {}).get('provider')!r} is not yfinance")
    if doc.get("status") != "approved" or doc.get("approval_rule_version") != "approval-v2-universe":
        problems.append(f"snapshot {doc.get('status')!r} under {doc.get('approval_rule_version')!r}, expected approved under approval-v2-universe")
    if {d.get("kind") for d in doc.get("bias_disclosures") or []} != DISCLOSURE_KINDS:
        problems.append("hindsight/survivorship disclosures missing")
    if (doc.get("coverage") or {}).get("start") != UNIVERSE_HISTORY_START:
        problems.append(f"coverage starts {(doc.get('coverage') or {}).get('start')!r}, expected {UNIVERSE_HISTORY_START}")
    summary = doc.get("observation_summary") or {}
    if not summary.get("instruments_expected") or summary.get("instruments_complete") != summary.get("instruments_expected"):
        problems.append(f"instruments complete {summary.get('instruments_complete')}/{summary.get('instruments_expected')}")
    if problems:
        raise SmokeError(f"universe snapshot {sid}: " + "; ".join(problems))
    log(f"smoke: universe snapshot {sid} approved (yfinance, {summary.get('instruments_complete')}/{summary.get('instruments_expected')}), trigger outcome {rec.get('outcome')} for {rec.get('session_date')}")
    return {"input_snapshot_id": sid, "session_date": rec.get("session_date"), "outcome": rec.get("outcome")}
