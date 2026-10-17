"""Prod smoke suite against a local deployment double (task 10.4; PIPE-05, local part).

The double is the deployed shape minus AWS: the same API router (``ApiApp``) over the DynamoDB
fake, moto S3 (presigned downloads served by moto) and the synthetic fixture provider for
on-demand ingestion. The smoke run must create, validate and publish a version and record a paper
execution on the synthetic smoke portfolio, verify read-back checksums, and leave every other plan
untouched. The deployed run (prod ``SmokeTests`` action) is BLOCKED by the bootstrap (task 10.6).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

import pytest
import requests
from finplan_platform.core.config import load_config
from finplan_platform.core.ingestion_budget import StaticBudgetGate
from finplan_platform.core.repository import MetadataRepository
from finplan_platform.core.services import Services
from finplan_platform.handlers.api import ApiApp, LocalClient
from finplan_platform.providers.fixture import FixtureProvider

from scripts import stage_runner
from tests.smoke.smoke_suite import (
    SMOKE_CONTENT,
    UNIVERSE_SMOKE_CONTENT,
    MemoryState,
    SmokeError,
    UniversePending,
    check_universe,
    content_checksum,
    run_smoke,
)
from tests.smoke.transport import SigV4Transport

OPERATOR = "arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-operator-pipeline-stage"
WEBSITE = "arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-website-backend"


class LocalTransport:
    """The smoke Transport over the in-process router (the deployment double)."""

    def __init__(self, app: ApiApp, principal: str) -> None:
        self.client = LocalClient(app, principal)
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any], dict[str, str]]:
        self.calls.append((method, path))
        return self.client.call(method, path, body if method != "GET" else None)

    def download(self, url: str) -> bytes:
        resp = requests.get(url, timeout=5)  # moto serves the presigned grant; no network
        assert resp.status_code == 200
        return resp.content


@pytest.fixture
def double(ddb: Any, artifacts: Any, clock: Any) -> ApiApp:
    from finplan_platform.core.ids import IdFactory

    cfg = load_config("beta")
    provider = FixtureProvider(dataset_id=cfg.dataset_id, instrument_id=str(cfg.dataset["instrument"]), clock=clock)
    svc = Services(cfg=cfg, repo=MetadataRepository(ddb, "beta"), artifacts=artifacts, extras={"ingestion_provider": provider, "budget_gate": StaticBudgetGate(None)})
    return ApiApp(svc, clock=clock, ids=IdFactory(clock))


def _items(ddb: Any, table: str) -> list[dict[str, Any]]:
    return ddb.scan(TableName=f"finplan-beta-financialplanning-{table}")["Items"]


def test_prod_smoke_on_the_synthetic_portfolio_PIPE_05(double: ApiApp, ddb: Any, clock: Any) -> None:
    # an unrelated plan that smoke must never touch
    web = LocalClient(double, WEBSITE)
    _, pf, _ = web.post("/v1/portfolios", {"name": "Someone's plan", "base_currency": "USD", "synthetic": True, "idempotency_key": "other-pf"})
    _, other, _ = web.post(f"/v1/portfolios/{pf['portfolio_id']}/plans", {"name": "other", "expected_revision": pf["revision"], "idempotency_key": "other-pl"})
    before = json.dumps(sorted(_items(ddb, "plan"), key=str), sort_keys=True, default=str)

    state = MemoryState()
    t = LocalTransport(double, OPERATOR)
    lines: list[str] = []
    result = run_smoke(t, state, run_key="rel-a", dataset_id="finance/etf-daily/SPY", today=date(2026, 1, 12), log=lines.append)
    assert state.get() == result.portfolio_id and result.portfolio_id != pf["portfolio_id"]
    assert result.checksum == content_checksum(SMOKE_CONTENT)
    assert result.execution_id.startswith("exe_") and result.publication_id.startswith("pub_")
    assert any("read back" in line for line in lines)

    # every write went to the smoke portfolio's own plan
    writes = [(m, p) for m, p in t.calls if m == "POST"]
    assert all(p.startswith(("/v1/portfolios", "/v1/ingestions", f"/v1/plans/{result.plan_id}", f"/v1/plan-versions/{result.plan_version_id}", f"/v1/publications/{result.publication_id}")) for _, p in writes)
    assert not any(other["plan_id"] in p for _, p in t.calls)
    def other_record(items: list[dict[str, Any]]) -> dict[str, Any]:
        return next(i for i in items if other["plan_id"] in json.dumps(i, default=str))

    assert other_record(_items(ddb, "plan")) == other_record(json.loads(before))

    # every record smoke created is synthetic
    for table in ("portfolio", "plan-version", "publication", "execution"):
        for item in _items(ddb, table):
            assert "synthetic" in json.dumps(item, default=str)

    # a second run (next release) reuses the dedicated portfolio
    clock.advance(hours=1)
    second = run_smoke(LocalTransport(double, OPERATOR), state, run_key="rel-b", dataset_id="finance/etf-daily/SPY", today=date(2026, 1, 12), log=lambda _m: None)
    assert second.portfolio_id == result.portfolio_id and second.plan_id != result.plan_id


class FakeTransport:
    def __init__(self, responses: dict[tuple[str, str], tuple[int, dict[str, Any]]]) -> None:
        self.responses = responses

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any], dict[str, str]]:
        code, payload = self.responses[(method, path)]
        return code, payload, {}

    def download(self, url: str) -> bytes:  # pragma: no cover - not reached
        raise AssertionError


def test_smoke_never_touches_a_non_synthetic_portfolio() -> None:
    t = FakeTransport({("GET", "/v1/portfolios/pf_X"): (200, {"portfolio_id": "pf_X", "synthetic": False, "revision": 1})})
    with pytest.raises(SmokeError, match="not synthetic"):
        run_smoke(t, MemoryState("pf_X"), run_key="r", dataset_id="finance/etf-daily/SPY", today=date(2026, 1, 12), log=lambda _m: None)


def test_smoke_reports_api_errors_with_their_code() -> None:
    t = FakeTransport({("POST", "/v1/portfolios"): (403, {"code": "FORBIDDEN", "message": "denied"})})
    with pytest.raises(SmokeError, match="FORBIDDEN"):
        run_smoke(t, MemoryState(), run_key="r", dataset_id="finance/etf-daily/SPY", today=date(2026, 1, 12), log=lambda _m: None)


# ------------------------------------------------------------------ phase 2 (decision 26)
UNIVERSE_SID = "snap_01M4DKRX5EYP18VESM7P5GWAB9"


def _universe_snapshot(**over: Any) -> dict[str, Any]:
    snap = {
        "input_snapshot_id": UNIVERSE_SID,
        "status": "approved",
        "approval_rule_version": "approval-v2-universe",
        "lineage": {"provider": "yfinance", "library_version": "1.7.0"},
        "coverage": {"start": "2010-10-01", "end": "2026-10-07"},
        "observation_summary": {"instruments_expected": 5, "instruments_complete": 5},
        "bias_disclosures": [{"kind": "hindsight_selection", "text": "h"}, {"kind": "survivorship", "text": "s"}],
    }
    snap.update(over)
    return snap


class Phase2Transport:
    """Scripted plan API for the phase 2 smoke path; records every call."""

    def __init__(self, snapshot: dict[str, Any], outcome: str = "skipped_no_strategy", outcome_day: str = "2026-10-07") -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.snapshot, self.outcome, self.outcome_day = snapshot, outcome, outcome_day
        self.content: Any = None

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any], dict[str, str]]:
        self.calls.append((method, path, body))
        if path.startswith("/v1/daily-trigger/outcomes/"):
            day = path.rsplit("/", 1)[-1]
            recs = [{"outcome": self.outcome, "session_date": day, "input_snapshot_id": UNIVERSE_SID, "published": False, "recorded_at": f"{day}T13:00:22Z"}] if day == self.outcome_day else []
            return 200, {"session_date": day, "outcomes": recs}, {}
        if path == f"/v1/snapshots/{UNIVERSE_SID}":
            return 200, {"snapshot": self.snapshot}, {}
        if (method, path) == ("GET", "/v1/portfolios/pf_S"):
            return 200, {"portfolio": {"portfolio_id": "pf_S", "synthetic": True, "revision": 1}}, {}
        if (method, path) == ("POST", "/v1/portfolios/pf_S/plans"):
            return 201, {"plan_id": "pl_S"}, {}
        if (method, path) == ("GET", "/v1/plans/pl_S"):
            return 200, {"plan": {"head": {"revision": 0}, "publication_revision": 0}}, {}
        if (method, path) == ("POST", "/v1/plans/pl_S/versions"):
            self.content = body["content"]
            return 201, {"plan_version_id": "pv_S", "checksum": content_checksum(body["content"])}, {}
        checksum = content_checksum(self.content)
        if path.startswith("/v1/plan-versions/pv_S") and method == "GET":
            return 200, {"plan_version": {"checksum": checksum}, "content_ref": {"checksum": checksum}, "download_grant": {"url": "https://example.invalid/pv_S"}}, {}
        if path == "/v1/plan-versions/pv_S/validate":
            return 200, {"status": "validated"}, {}
        if path == "/v1/plans/pl_S/publications":
            return 201, {"publication_id": "pub_S", "plan_version_checksum": checksum}, {}
        if path == "/v1/publications/pub_S":
            return 200, {"publication_id": "pub_S"}, {}
        if path == "/v1/publications/pub_S/executions":
            return 201, {"execution_id": "exe_S", "mode": "paper", "publication_id": "pub_S"}, {}
        if path == "/v1/executions/exe_S":
            return 200, {"execution_id": "exe_S"}, {}
        raise AssertionError((method, path))

    def download(self, url: str) -> bytes:
        from finplan_contracts.canonical import canonicalize

        return canonicalize(self.content)


def test_phase2_smoke_reuses_the_scheduled_universe_snapshot_without_ingestion() -> None:
    t = Phase2Transport(_universe_snapshot())
    result = run_smoke(t, MemoryState("pf_S"), run_key="r", dataset_id="finance/etf-daily/SPY", today=date(2026, 10, 8), log=lambda _m: None, phase=2)
    assert result.input_snapshot_id == UNIVERSE_SID and result.execution_id == "exe_S"
    assert result.checksum == content_checksum(UNIVERSE_SMOKE_CONTENT)
    assert not any(p == "/v1/ingestions" for _, p, _ in t.calls), "phase 2 smoke must never start an ingestion (no real-provider call)"
    version_body = next(b for m, p, b in t.calls if (m, p) == ("POST", "/v1/plans/pl_S/versions"))
    assert version_body["input_snapshot_id"] == UNIVERSE_SID


def test_phase2_universe_check_passes_on_an_approved_real_snapshot() -> None:
    found = check_universe(Phase2Transport(_universe_snapshot()), date(2026, 10, 8), log=lambda _m: None)
    assert found == {"input_snapshot_id": UNIVERSE_SID, "session_date": "2026-10-07", "outcome": "skipped_no_strategy"}
    assert check_universe(Phase2Transport(_universe_snapshot(), outcome="pending_approval"), date(2026, 10, 8), log=lambda _m: None)["outcome"] == "pending_approval"


@pytest.mark.parametrize(
    ("snapshot", "outcome", "needle"),
    [
        (_universe_snapshot(status="committed"), "skipped_no_strategy", "expected approved"),
        (_universe_snapshot(lineage={"provider": "fixture"}), "skipped_no_strategy", "not yfinance"),
        (_universe_snapshot(bias_disclosures=[]), "skipped_no_strategy", "disclosures"),
        (_universe_snapshot(observation_summary={"instruments_expected": 5, "instruments_complete": 4}), "skipped_no_strategy", "4/5"),
        (_universe_snapshot(), "skipped_snapshot", "trigger outcome"),
    ],
)
def test_phase2_universe_check_fails_on_defects(snapshot: dict[str, Any], outcome: str, needle: str) -> None:
    with pytest.raises(SmokeError, match=needle):
        check_universe(Phase2Transport(snapshot, outcome=outcome), date(2026, 10, 8), log=lambda _m: None)


def test_phase2_universe_check_is_pending_while_the_latest_snapshot_is_a_fixture_one() -> None:
    with pytest.raises(UniversePending):
        check_universe(Phase2Transport(_universe_snapshot(synthetic=True)), date(2026, 10, 8), log=lambda _m: None)
    with pytest.raises(SmokeError, match="no scheduled universe snapshot"):
        check_universe(Phase2Transport(_universe_snapshot(), outcome_day="2026-09-01"), date(2026, 10, 8), log=lambda _m: None)


# ------------------------------------------------------------------ deployed transport (no network)
def test_sigv4_transport_signs_requests_without_network() -> None:
    from botocore.credentials import Credentials

    sent: list[Any] = []

    class Resp:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def opener(req: Any, timeout: float) -> Resp:
        sent.append(req)
        return Resp()

    t = SigV4Transport("https://example.invalid/live", "us-east-2", Credentials("testing-fake-key", "testing-fake-secret"), opener=opener)
    code, body, _ = t.call("POST", "/v1/portfolios", {"name": "x"})
    assert code == 200 and body == {"ok": True}
    req = sent[0]
    assert req.full_url == "https://example.invalid/live/v1/portfolios" and req.get_method() == "POST"
    auth = req.get_header("Authorization")
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=testing-fake-key/") and "/us-east-2/execute-api/aws4_request" in auth
    with pytest.raises(ValueError):
        SigV4Transport("http://example.invalid", "us-east-2", None)


@pytest.mark.parametrize("dataset", ["finance/equity-etf-daily/research-universe", "finance%2Fequity-etf-daily%2Fresearch-universe"])
def test_sigv4_query_signature_matches_independent_parameter_canonicalization(dataset: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    monkeypatch.setattr("botocore.auth.get_current_datetime", lambda: datetime(2026, 10, 10, 12, tzinfo=UTC))
    credentials = Credentials("testing-fake-key", "testing-fake-secret", "testing-fake-token")
    transport = SigV4Transport("https://example.invalid/live", "us-east-2", credentials)
    request = transport.signed_request("GET", f"/v1/snapshots/latest?dataset_id={dataset}&cursor=a%2Bb%2F%3D&tag=two%20words&empty=&tag=one")
    url = urlsplit(request.full_url)
    values = parse_qsl(url.query, keep_blank_values=True)
    assert ("dataset_id", "finance/equity-etf-daily/research-universe") in values
    assert ("cursor", "a+b/=") in values and ("empty", "") in values
    assert "dataset_id=finance%2Fequity-etf-daily%2Fresearch-universe" in url.query
    assert " " not in url.query and "/" not in url.query and "+" not in url.query
    # A server canonicalizes decoded parameters independently of the original
    # URL spelling. Botocore's params path URI-encodes these values itself.
    server_headers = {k: v for k, v in request.header_items() if k.lower() != "authorization"}
    server = AWSRequest(method="GET", url=urlunsplit(url._replace(query="")), params=values, headers=server_headers)
    SigV4Auth(credentials, "execute-api", "us-east-2").add_auth(server)
    assert request.get_header("Authorization") == server.headers["Authorization"]


# ------------------------------------------------------------------ stage runner
class Proc:
    def __init__(self, rc: int) -> None:
        self.returncode = rc


def _runner(rc: int, tests: int, skipped: int, seen: list[dict[str, Any]]) -> Any:
    def run(cmd: list[str], cwd: Any, env: dict[str, str]) -> Proc:
        seen.append({"cmd": cmd, "env": env})
        junit = next(c.split("=", 1)[1] for c in cmd if c.startswith("--junitxml="))
        with open(junit, "w") as fh:
            fh.write(f'<testsuites><testsuite name="s" tests="{tests}" skipped="{skipped}" failures="0" errors="0"/></testsuites>')
        return Proc(rc)

    return run


def test_stage_runner_selects_the_environment_suite() -> None:
    seen: list[dict[str, Any]] = []
    assert stage_runner.tests_action("prod", run=_runner(0, 1, 0, seen), environ={}) == 0
    assert "tests/smoke" in seen[0]["cmd"] and "not live_provider" in seen[0]["cmd"]
    assert seen[0]["env"]["FINPLAN_SUITE"] == "smoke" and seen[0]["env"]["FINPLAN_TARGET_ENV"] == "prod"
    assert stage_runner.tests_action("beta", run=_runner(0, 2, 0, seen), environ={}) == 0
    assert seen[1]["env"]["FINPLAN_SUITE"] == "integration-beta" and "tests/integration" in seen[1]["cmd"]


@pytest.mark.parametrize("env", ["beta", "gamma", "prod"])
def test_a_stage_suite_that_executes_nothing_fails_PIPE_05(env: str) -> None:
    """Every environment suite must execute at least one test (the beta/gamma false-pass incident)."""
    assert stage_runner.tests_action(env, run=_runner(0, 1, 1, []), environ={}) == 1  # all skipped
    assert stage_runner.tests_action(env, run=_runner(5, 0, 0, []), environ={}) == 1  # nothing collected
    assert stage_runner.tests_action(env, run=_runner(0, 0, 0, []), environ={}) == 1
    assert stage_runner.tests_action(env, run=_runner(1, 3, 0, []), environ={}) == 1  # failures always fail
    assert stage_runner.tests_action(env, run=_runner(0, 4, 1, []), environ={}) == 0


def test_downloaded_content_hash_matches(double: ApiApp) -> None:
    """Read-back proof used by the smoke run: the grant bytes hash to the version checksum."""
    t = LocalTransport(double, OPERATOR)
    result = run_smoke(t, MemoryState(), run_key="rel-c", dataset_id="finance/etf-daily/SPY", today=date(2026, 1, 12), log=lambda _m: None)
    _, got, _ = t.call("GET", f"/v1/plan-versions/{result.plan_version_id}?download=true")
    assert "sha256:" + hashlib.sha256(t.download(got["download_grant"]["url"])).hexdigest() == result.checksum
