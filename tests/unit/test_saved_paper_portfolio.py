"""Saved paper state is persisted, audited, revision checked and read only to inference."""
from __future__ import annotations

import copy
import pytest
from finplan_contracts.validate import validate
from finplan_platform.core.plans import create_portfolio, get_portfolio_state, put_portfolio_state
from finplan_platform.core.repository import MetadataRepository
from finplan_platform.core.services import Services
from finplan_platform.handlers.api import match_route
from tests.api_support import *


def state():
    return {"positions": [{"instrument_id": "SPY", "quantity": 20.5}], "cash_balance": 1000, "high_watermark": 10000, "as_of": "2026-01-09", "base_currency": "USD", "mode": "paper", "source": "paper_initialization"}


def request(**changes):
    return {"paper_state": state(), "expected_revision": 0, "idempotency_key": "paper-initialization", **changes}


def test_paper_state_on_legacy_portfolio_is_atomic_and_independent_of_plan_revision(clients, flow, svc):
    pf = flow.portfolio()
    flow.plan(portfolio=pf)
    code, first, headers = clients.operator.call("PUT", f"/v1/portfolios/{pf['portfolio_id']}/state", request())
    assert code == 200 and first["revision"] == 1 and first["paper_state"] == state()
    assert validate(first, "api/get-portfolio-state-response").valid
    record = svc.repo.require("portfolio", pf["portfolio_id"])
    assert record.attrs["revision"] == 2 and record.attrs["paper_state_revision"] == 1
    assert "paper_state" not in record.doc
    code, replay, headers = clients.operator.call("PUT", f"/v1/portfolios/{pf['portfolio_id']}/state", request())
    assert code == 200 and replay == first and headers["X-Idempotent-Replay"] == "true"
    code, stale, _ = clients.operator.call("PUT", f"/v1/portfolios/{pf['portfolio_id']}/state", request(idempotency_key="different-key"))
    assert code == 409 and stale["code"] == "CONFLICT"
    updated = state(); updated["cash_balance"] = 900
    code, second, _ = clients.operator.call("PUT", f"/v1/portfolios/{pf['portfolio_id']}/state", request(paper_state=updated, expected_revision=1, idempotency_key="manual-change"))
    assert code == 200 and second["revision"] == 2
    audit = [e.to_doc() for e in svc.repo.audit_events(pf["portfolio_id"])]
    events = [e for e in audit if e["operation"] == "put_portfolio_state"]
    assert len(events) == 2 and events[0]["new"]["paper_state"] == state() and events[1]["prior"]["revision"] == 1


@pytest.mark.parametrize("principal", ["reader", "submitter", "writer", "fm_job_api", "website"])
def test_state_reader_permissions_and_no_write(clients, flow, principal):
    pf = flow.portfolio(); path = f"/v1/portfolios/{pf['portfolio_id']}/state"
    assert clients.operator.call("PUT", path, request())[0] == 200
    assert getattr(clients, principal).get(path)[0] == 200
    code, body, _ = getattr(clients, principal).call("PUT", path, request(expected_revision=1,idempotency_key="deny-write"))
    assert code == 403 and body["code"] == "FORBIDDEN"
    assert clients.operator.get(path)[1]["revision"] == 1


def test_missing_state_is_explicit_and_cross_env_denied(clients, flow):
    pf = flow.portfolio(); path = f"/v1/portfolios/{pf['portfolio_id']}/state"
    code, body, _ = clients.reader.get(path)
    assert code == 422 and body["code"] == "PRECONDITION_FAILED" and body["details"]["reason"] == "portfolio_state_missing"
    assert clients.gamma_reader.get(path)[0] == 403
    assert clients.fm_job.get(path)[0] == 403


@pytest.mark.parametrize("mutate", [lambda s:s.update(cash_balance=-1),lambda s:s.update(mode="live"),lambda s:s["positions"].append(s["positions"][0].copy()),lambda s:s.update(as_of="2027-01-01"),lambda s:s.update(high_watermark=10)])
def test_invalid_state_has_no_write(clients, flow, mutate):
    pf = flow.portfolio(); path=f"/v1/portfolios/{pf['portfolio_id']}/state"; s=state();mutate(s)
    assert clients.operator.call("PUT",path,request(paper_state=s))[0] == 400
    assert clients.operator.get(path)[1]["details"]["reason"] == "portfolio_state_missing"


def test_reused_key_cannot_change_state(clients, flow):
    pf=flow.portfolio();path=f"/v1/portfolios/{pf['portfolio_id']}/state"
    assert clients.operator.call("PUT",path,request())[0] == 200
    s=state();s["cash_balance"]=900
    code,body,_=clients.operator.call("PUT",path,request(paper_state=s))
    assert code==422 and body["code"]=="IDEMPOTENCY_KEY_REUSED"


def test_paper_state_moto_and_fake_persistence(repo_any, artifacts, ctx_factory):
    from finplan_platform.core.config import load_config
    svc=Services(cfg=load_config("beta"),repo=repo_any,artifacts=artifacts)
    ctx=ctx_factory(role_class="operator")
    pf=create_portfolio(ctx,svc,{"name":"Paper","base_currency":"USD","idempotency_key":"pf-state"}).response
    result=put_portfolio_state(ctx,svc,pf["portfolio_id"],request()).response
    assert get_portfolio_state(ctx,svc,pf["portfolio_id"])==result
    assert repo_any.require("portfolio",pf["portfolio_id"]).attrs["revision"]==1


def test_latest_snapshot_static_route_filters_newer_unapproved(clients, svc, clock):
    sid=seed_snapshot(svc,clock)
    dataset=svc.repo.require("snapshot_catalog",sid).doc["dataset"]["dataset_id"]
    clock.advance(seconds=1);seed_snapshot(svc,clock,status="committed")
    clock.advance(seconds=1);seed_snapshot(svc,clock,status="expired")
    assert match_route("GET","/v1/snapshots/latest")[0].operation=="latest_approved_snapshot"
    for principal in ("reader","fm_job","fm_job_api"):
        code,body,_=getattr(clients,principal).get(f"/v1/snapshots/latest?dataset_id={dataset}")
        assert code==200 and body["snapshot"]["input_snapshot_id"]==sid
        assert body["snapshot"]["status"]=="approved"


def test_latest_snapshot_absence_and_validation(clients):
    code,body,_=clients.reader.get("/v1/snapshots/latest?dataset_id=finance/missing/data")
    assert code==404 and body["code"]=="NOT_FOUND" and body["details"]["reason"]=="approved_snapshot_missing"
    assert clients.reader.get("/v1/snapshots/latest")[0]==400
    assert clients.reader.get("/v1/snapshots/latest?dataset_id=bad")[0]==400
    assert clients.gamma_reader.get("/v1/snapshots/latest?dataset_id=finance/missing/data")[0]==403


def test_inference_can_read_research_plan_to_resolve_default_portfolio(clients,flow):
    plan=flow.plan()
    code,body,_=clients.fm_job_api.get(f"/v1/plans/{plan['plan_id']}")
    assert code==200 and body["plan"]["portfolio_id"]==plan["portfolio_id"]
