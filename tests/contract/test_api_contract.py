"""Plan API contract suite (tasks 4.2, 4.4, 4.5, 5.4; API-02, API-04, API-09, API-10; CS-01).

* API-02: every API-owned route is exercised; each response validates against its contract
  (or platform) schema and every error against ``core/v1/error.json``; the pinned package's
  own conformance suite passes in consumer mode at the pinned version; no contract schema is
  copied into this repository (copied-``$id`` check).
* API-10: records written under an older minor are served unchanged by a newer build
  (``contract_version`` echoed, checksum and stored bytes unchanged, read-time defaults only),
  using the package's schema-upgrade fixtures; a new major is served alongside the previous one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate
from finplan_platform.core import upgrade
from finplan_platform.core.contract_io import validate_document
from finplan_platform.core.repository import PutNew
from finplan_platform.core.snapshot_reads import load_payloads
from finplan_platform.handlers.api import ROUTES, match_route

from tests.api_support import *
from tests.api_support import PRINCIPALS, Flow, content, seed_snapshot
from tests.fakes.records import version_doc

REPO = Path(__file__).resolve().parents[2]


class Recorder:
    """Wraps clients and records (route, status, body) for every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, int, dict[str, Any]]] = []

    def wrap(self, client: Any) -> Any:
        rec = self

        class _C:
            def call(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
                code, resp, headers = client.call(method, path, body, **kw)
                found = match_route(method, path.split("?")[0])
                rec.calls.append((found[0] if found else None, code, resp))
                return code, resp, headers

            def get(self, path: str, **kw: Any) -> Any:
                return self.call("GET", path, **kw)

            def post(self, path: str, body: Any = None, **kw: Any) -> Any:
                return self.call("POST", path, {} if body is None else body, **kw)

        return _C()


def test_every_api_route_conforms(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    rec = Recorder()
    web, tool, writer, op = rec.wrap(clients.website), rec.wrap(clients.reader), rec.wrap(clients.writer), rec.wrap(clients.operator)
    snap = seed_snapshot(svc, clock)
    _, pf, _ = web.post("/v1/portfolios", {"name": "Contract portfolio", "base_currency": "USD", "idempotency_key": "c-pf"})
    tool.get(f"/v1/portfolios/{pf['portfolio_id']}")
    op.call("PUT", f"/v1/portfolios/{pf['portfolio_id']}/state", {"paper_state": {"positions": [{"instrument_id": "SPY", "quantity": 20.5}], "cash_balance": 1000, "high_watermark": 10000, "as_of": "2026-01-09", "base_currency": "USD", "mode": "paper", "source": "paper_initialization"}, "expected_revision": 0, "idempotency_key": "c-paper-state"})
    tool.get(f"/v1/portfolios/{pf['portfolio_id']}/state")
    dataset_id = svc.repo.require("snapshot_catalog", snap).doc["dataset"]["dataset_id"]
    tool.get(f"/v1/snapshots/latest?dataset_id={dataset_id}")
    _, plan, _ = web.post(f"/v1/portfolios/{pf['portfolio_id']}/plans", {"name": "Contract plan", "expected_revision": 1, "idempotency_key": "c-pl"})
    pid = plan["plan_id"]
    _, root, _ = web.post(f"/v1/plans/{pid}/versions", flow.root_body(snap, expected_revision=1, key="c-root"))
    _, child, _ = writer.post(f"/v1/plans/{pid}/versions", flow.child_body(root["plan_version_id"], expected_revision=2, key="c-child", body_content=content(fees={"transaction_cost_bps": 2})))
    writer.post(f"/v1/plan-versions/{root['plan_version_id']}/validate", {"idempotency_key": "c-v1"})
    writer.post(f"/v1/plan-versions/{child['plan_version_id']}/validate", {"idempotency_key": "c-v2"})
    tool.get(f"/v1/plan-versions/{child['plan_version_id']}")
    tool.get(f"/v1/plan-versions/{child['plan_version_id']}?download=true")
    tool.get(f"/v1/plans/{pid}")
    tool.get(f"/v1/plans/{pid}/versions?page_size=1")
    _, pub, _ = writer.post(f"/v1/plans/{pid}/publications", {"plan_version_id": child["plan_version_id"], "expected_revision": 1, "idempotency_key": "c-pub"})
    tool.get(f"/v1/publications/{pub['publication_id']}")
    tool.get(f"/v1/plans/{pid}/publications")
    tool.get(f"/v1/publications/{pub['publication_id']}/executions")
    _, exe, _ = op.post(f"/v1/publications/{pub['publication_id']}/executions", {"mode": "paper", "idempotency_key": "c-exe"})
    tool.get(f"/v1/executions/{exe['execution_id']}")
    tool.get(f"/v1/snapshots/{snap}")
    tool.get(f"/v1/snapshots/{snap}/observations?instrument_id=SPY&start_date=2025-12-31&end_date=2026-01-06")
    # The issued-decision lifecycle uses the same stored snapshot and book, and
    # every new route is validated with the installed contract package.
    model = rec.wrap(clients.fm_job_api)
    observations = load_payloads(svc, svc.repo.require("snapshot_catalog", snap))[0]["observations"]
    observation = max(observations, key=lambda item: item["session_date"])
    proposal = {"portfolio_id": pf["portfolio_id"], "algorithm_family": "optimization", "algorithm": "min_variance",
                "input_snapshot_id": snap, "portfolio_revision": 1,
                "recommendation": {"target_weights": {"SPY": 0.6, "USD_CASH": 0.4}}, "provenance": {"model_version": "fixed-solver"},
                "execution": {"reference_date": observation["session_date"], "reference_prices": {"SPY": observation["close"]},
                              "target_weights": {"SPY": 0.6, "USD_CASH": 0.4}, "transaction_cost_bps": 2}, "idempotency_key": "c-decision"}
    _, issued, _ = model.post(f"/v1/portfolios/{pf['portfolio_id']}/decisions", proposal)
    decision_id = issued["decision"]["decision_id"]
    tool.get(f"/v1/portfolio-decisions/{decision_id}")
    tool.get(f"/v1/portfolios/{pf['portfolio_id']}/decisions")
    op.post(f"/v1/portfolio-decisions/{decision_id}/resolution", {"action": "accept", "expected_revision": 1, "confirmed_by_user": True, "idempotency_key": "c-resolution"})
    tool.get(f"/v1/portfolios/{pf['portfolio_id']}/history")
    tool.get(f"/v1/portfolios/{pf['portfolio_id']}/history/2")
    tool.get(f"/v1/snapshots?dataset_id={dataset_id}")
    tool.post("/v1/activity-events", {"event_kind": "contract_test", "portfolio_id": pf["portfolio_id"], "session_id": "contract-session",
                                    "correlation_id": "cor_contract_receipt", "payload": {"decision_id": decision_id}, "idempotency_key": "c-activity"}, headers={"X-Correlation-Id": "cor_contract_receipt"})
    tool.get(f"/v1/activity-events?portfolio_id={pf['portfolio_id']}")
    # error paths
    writer.post(f"/v1/plans/{pid}/publications", {"plan_version_id": child["plan_version_id"], "expected_revision": 1, "idempotency_key": "c-pub2"})  # CONFLICT
    op.post(f"/v1/publications/{pub['publication_id']}/executions", {"mode": "live", "idempotency_key": "c-live"})
    tool.get("/v1/plan-versions/pl_01KDVDNAZ83BAMMYCEGWF33DPM")
    web.post("/v1/portfolios", {"name": "x", "base_currency": "USD", "idempotency_key": "c-v", "contract_version": "9.0.0"})

    exercised = {c[0].operation for c in rec.calls if c[0] is not None and 200 <= c[1] < 300}
    api_owned = {r.operation for r in ROUTES if r.owner == "api"}
    assert api_owned <= exercised, api_owned - exercised
    errors = 0
    for route, code, body in rec.calls:
        if code >= 400:
            errors += 1
            assert validate(body, "error").valid, body
            continue
        schema = route.response_schema(body) if callable(route.response_schema) else route.response_schema
        assert validate_document(body, schema).valid, (route.operation, body)
    assert errors == 4
    # stored records conform to the package schemas (MDS-01 "stored record conforms", API-04 contract)
    for table, schema in (("plan_version", "plan-version"), ("publication", "publication"), ("execution", "execution"), ("portfolio", "portfolio")):
        for r in svc.repo.scan(table):
            assert validate(upgrade.strip_served(upgrade.serve(r)), schema).valid, table


def test_package_conformance_suite_passes_at_the_pinned_version() -> None:
    from finplan_contracts.conformance import run_consumer

    pin = json.loads((REPO / "contracts-pin.json").read_text())
    version = pin.get("version") or pin.get("finplan_contracts", {}).get("version") or load_store().version
    report = run_consumer(expect_version=version)
    assert report.ok, [str(p) for p in report.problems][:10]


def test_no_contract_schema_is_copied_into_the_service() -> None:
    from finplan_contracts.copied_id import scan_tree

    for part in ("platform", "infra", "docs", "tests", "config"):
        found = scan_tree(REPO / part)
        assert found == [], [str(f) for f in found]


# ------------------------------------------------------------------ API-10
@pytest.fixture
def newer_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a platform release pinned to contracts 1.1.0 (the package data stays the pinned one)."""
    monkeypatch.setattr(upgrade, "CURRENT_VERSION_OVERRIDE", "1.1.0")


def _stored(ddb: Any, table: str, pk: str) -> dict[str, Any]:
    return ddb.get_item(TableName=f"finplan-beta-financialplanning-{table}", Key={"pk": {"S": pk}})["Item"]


def test_older_minor_plan_version_is_served_unchanged(newer_build: None, flow: Flow, clients: Any, svc: Any, ctx: Any, ddb: Any) -> None:
    plan = flow.plan()
    old = version_doc(ctx, plan["plan_id"])  # a 1.0.0-era record: no no_effect, no content_ref
    old.pop("no_effect", None)
    svc.repo.commit([PutNew("plan_version", doc=old, contract_version="1.0.0")])
    before = _stored(ddb, "plan-version", old["plan_version_id"])
    code, got, _ = clients.reader.get(f"/v1/plan-versions/{old['plan_version_id']}")
    assert code == 200, got
    pv = got["plan_version"]
    assert pv["contract_version"] == "1.0.0" and pv["checksum"] == old["checksum"]
    assert pv["no_effect"] is False  # additive field defaulted at read time
    assert {k: v for k, v in pv.items() if k not in ("contract_version", "no_effect")} == old
    assert _stored(ddb, "plan-version", old["plan_version_id"]) == before  # never rewritten
    assert validate(upgrade.strip_served(pv), "plan-version").valid
    # writers stamp the current version
    _, pf, _ = clients.website.post("/v1/portfolios", {"name": "New", "base_currency": "USD", "idempotency_key": "up-1", "contract_version": "1.0.0"})
    assert _stored(ddb, "portfolio", pf["portfolio_id"])["contract_version"] == {"S": "1.1.0"} and pf["contract_version"] == "1.1.0"


def test_package_schema_upgrade_fixtures_are_served_unchanged(newer_build: None, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    store = load_store()
    snap_fixture = json.loads((store.fixtures_dir("input-snapshot") / "valid" / "schema-upgrade-without-provider-lineage-fields.json").read_text())
    obs_fixture = json.loads((store.fixtures_dir("observation") / "valid" / "schema-upgrade-without-optional-fields.json").read_text())
    payload = json.loads((store.fixtures_dir("snapshot-payload") / "valid" / "etf-daily.json").read_text())
    payload["observations"] = [o for o in payload["observations"] if o["session_date"] != obs_fixture["session_date"]] + [obs_fixture]
    overrides = {k: v for k, v in snap_fixture.items() if k not in ("input_snapshot_id", "artifacts", "manifest_checksum", "created_at")}
    sid = seed_snapshot(svc, clock, status=snap_fixture["status"], contract_version="1.0.0", doc_overrides=overrides, payload=payload)
    before = _stored(ddb, "snapshot-catalog", sid)
    code, got, _ = clients.reader.get(f"/v1/snapshots/{sid}")
    assert code == 200, got
    snap = got["snapshot"]
    assert snap["contract_version"] == "1.0.0" and snap["lineage"] == snap_fixture["lineage"]
    assert validate(upgrade.strip_served(snap), "input-snapshot").valid
    code, obs, _ = clients.reader.get(f"/v1/snapshots/{sid}/observations?start_date={obs_fixture['session_date']}&end_date={obs_fixture['session_date']}")
    assert code == 200 and obs_fixture in obs["observations"]
    assert _stored(ddb, "snapshot-catalog", sid) == before


def test_a_new_major_is_served_alongside_the_previous(monkeypatch: pytest.MonkeyPatch, flow: Flow, clients: Any, svc: Any, ctx: Any) -> None:
    monkeypatch.setattr(upgrade, "CURRENT_VERSION_OVERRIDE", "2.0.0")
    monkeypatch.setattr(upgrade, "ADDITIONAL_SERVED_MAJORS", (1,))
    plan = flow.plan()
    old = version_doc(ctx, plan["plan_id"])
    svc.repo.commit([PutNew("plan_version", doc=old, contract_version="1.4.0")])
    assert clients.reader.get(f"/v1/plan-versions/{old['plan_version_id']}")[0] == 200
    assert clients.reader.get(f"/v1/plans/{plan['plan_id']}", headers={"X-Finplan-Contract-Version": "1.4.0"})[0] == 200
    code, err, _ = clients.reader.get(f"/v1/plans/{plan['plan_id']}", headers={"X-Finplan-Contract-Version": "0.9.0"})
    assert code == 400 and err["details"]["served_contract_majors"] == [1, 2]
    unserved = version_doc(ctx, plan["plan_id"])
    svc.repo.commit([PutNew("plan_version", doc=unserved, contract_version="0.9.0")])
    code, err, _ = clients.reader.get(f"/v1/plan-versions/{unserved['plan_version_id']}")
    assert code == 400 and err["code"] == "UNSUPPORTED_CONTRACT_VERSION"


def test_principal_table_is_synthetic() -> None:
    from finplan_contracts.leak_scan import scan_text

    assert scan_text(json.dumps(PRINCIPALS), "principals.json") == []
