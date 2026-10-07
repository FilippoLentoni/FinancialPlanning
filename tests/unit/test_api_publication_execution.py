"""Publication and execution (tasks 5.2, 5.3; API-06, API-07; MDS-06 publication audit)."""

from __future__ import annotations

from typing import Any

from finplan_contracts.validate import validate

from tests.api_support import *
from tests.api_support import PRINCIPALS, Flow, content


def _pubs(ddb: Any) -> int:
    return len(ddb.scan(TableName="finplan-beta-financialplanning-publication")["Items"])


def _item(ddb: Any, table: str, pk: str) -> Any:
    return ddb.get_item(TableName=f"finplan-beta-financialplanning-{table}", Key={"pk": {"S": pk}})["Item"]


# ------------------------------------------------------------------ API-06
def test_publish_a_validated_version(flow: Flow, clients: Any, svc: Any) -> None:
    plan, _snap, root = flow.validated_plan()
    code, pub, _ = clients.writer.post(f"/v1/plans/{plan['plan_id']}/publications", {"plan_version_id": root["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-1"})
    assert code == 201 and validate(pub, "tools/publish-plan-version-response").valid
    assert pub["publication_id"].startswith("pub_") and pub["plan_version_checksum"] == root["checksum"]
    assert pub["supersedes_publication_id"] is None and pub["publication_revision"] == 2
    _, head, _ = clients.reader.get(f"/v1/plans/{plan['plan_id']}")
    assert head["plan"]["current_publication_id"] == pub["publication_id"] and head["plan"]["publication_revision"] == 2
    assert head["current_publication"]["publication_id"] == pub["publication_id"]
    events = svc.repo.audit_events(pub["publication_id"])
    assert len(events) == 1
    ev = events[0]
    assert ev.details["plan_version_id"] == root["plan_version_id"] and ev.details["checksum"] == root["checksum"]
    assert ev.caller["principal"] == PRINCIPALS["writer"] and ev.correlation_id.startswith("cor_")


def test_publishing_an_unvalidated_version_fails(flow: Flow, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    from tests.api_support import seed_snapshot

    snap = seed_snapshot(svc, clock)
    plan = flow.plan()
    v = flow.root(plan["plan_id"], snap)
    code, err, _ = clients.writer.post(f"/v1/plans/{plan['plan_id']}/publications", {"plan_version_id": v["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-pending"})
    assert code == 422 and err["code"] == "PRECONDITION_FAILED" and err["details"]["status"] == "pending_validation"
    assert _pubs(ddb) == 0


def test_publishing_a_version_of_another_plan_fails(flow: Flow, clients: Any) -> None:
    _plan, _snap, root = flow.validated_plan()
    other = flow.plan()
    code, err, _ = clients.writer.post(f"/v1/plans/{other['plan_id']}/publications", {"plan_version_id": root["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-x"})
    assert code == 400 and err["code"] == "VALIDATION_FAILED" and err["details"]["pointer"] == "/plan_version_id"


def test_a_new_publication_supersedes_without_modifying_the_previous(flow: Flow, clients: Any, ddb: Any) -> None:
    plan, _snap, root = flow.validated_plan()
    pid = plan["plan_id"]
    first = flow.publish(pid, root["plan_version_id"])
    stored_before = _item(ddb, "publication", first["publication_id"])
    _, got_before, _ = clients.reader.get(f"/v1/publications/{first['publication_id']}")
    child = flow.child(pid, root["plan_version_id"], body_content=content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.5}], "cash_weight": 0.5}))
    flow.validate(child["plan_version_id"])
    second = flow.publish(pid, child["plan_version_id"])
    assert second["supersedes_publication_id"] == first["publication_id"] and second["publication_revision"] == 3
    _, got_after, _ = clients.reader.get(f"/v1/publications/{first['publication_id']}")
    assert got_after == got_before and _item(ddb, "publication", first["publication_id"]) == stored_before
    assert validate(got_after, "publication").valid


def test_concurrent_publications_one_conflicts(flow: Flow, clients: Any, ddb: Any) -> None:
    plan, _snap, root = flow.validated_plan()
    pid = plan["plan_id"]
    child = flow.child(pid, root["plan_version_id"], body_content=content(fees={"transaction_cost_bps": 3}))
    flow.validate(child["plan_version_id"])
    other: list[Any] = []

    def interleave(_items: Any) -> None:
        ddb.before_transact_hooks.clear()
        other.append(clients.website.post(f"/v1/plans/{pid}/publications", {"plan_version_id": child["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-b"}))

    ddb.before_transact_hooks.append(interleave)
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/publications", {"plan_version_id": root["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-a"})
    assert other[0][0] == 201 and code == 409 and err["code"] == "CONFLICT"
    assert _pubs(ddb) == 1


def test_duplicate_publish_returns_the_original_publication(flow: Flow, clients: Any, ddb: Any) -> None:
    plan, _snap, root = flow.validated_plan()
    req = {"plan_version_id": root["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-dup"}
    _, a, _ = clients.writer.post(f"/v1/plans/{plan['plan_id']}/publications", req)
    _, b, h = clients.writer.post(f"/v1/plans/{plan['plan_id']}/publications", req)
    assert a["publication_id"] == b["publication_id"] and h["X-Idempotent-Replay"] == "true" and _pubs(ddb) == 1


# ------------------------------------------------------------------ API-07
def test_paper_execution_is_a_separate_record(flow: Flow, clients: Any, ddb: Any) -> None:
    plan, _snap, root = flow.validated_plan()
    pub = flow.publish(plan["plan_id"], root["plan_version_id"])
    pub_item = _item(ddb, "publication", pub["publication_id"])
    pv_item = _item(ddb, "plan-version", root["plan_version_id"])
    plan_item = _item(ddb, "plan", plan["plan_id"])
    code, exe, _ = clients.operator.post(f"/v1/publications/{pub['publication_id']}/executions", {"mode": "paper", "idempotency_key": "exe-1"})
    assert code == 201 and validate(exe, "execution").valid
    assert exe["execution_id"].startswith("exe_") and exe["publication_id"] == pub["publication_id"]
    assert exe["mode"] == "paper" and exe["status"] == "recorded" and exe["publication_superseded"] is False
    assert _item(ddb, "publication", pub["publication_id"]) == pub_item
    assert _item(ddb, "plan-version", root["plan_version_id"]) == pv_item
    assert _item(ddb, "plan", plan["plan_id"]) == plan_item
    _, got, _ = clients.reader.get(f"/v1/executions/{exe['execution_id']}")
    assert got["execution_id"] == exe["execution_id"] and validate(got, "execution").valid


def test_live_and_unknown_modes_are_not_permitted(flow: Flow, clients: Any, ddb: Any) -> None:
    plan, _snap, root = flow.validated_plan()
    pub = flow.publish(plan["plan_id"], root["plan_version_id"])
    for mode in ("live", "margin"):
        code, err, _ = clients.operator.post(f"/v1/publications/{pub['publication_id']}/executions", {"mode": mode, "idempotency_key": f"exe-{mode}"})
        assert code == 403 and err["code"] == "OPERATION_NOT_PERMITTED" and err["details"]["pointer"] == "/mode"
    assert len(ddb.scan(TableName="finplan-beta-financialplanning-execution")["Items"]) == 0


def test_execution_against_a_superseded_publication_is_flagged(flow: Flow, clients: Any) -> None:
    plan, _snap, root = flow.validated_plan()
    pid = plan["plan_id"]
    old = flow.publish(pid, root["plan_version_id"])
    child = flow.child(pid, root["plan_version_id"], body_content=content(fees={"transaction_cost_bps": 4}))
    flow.validate(child["plan_version_id"])
    flow.publish(pid, child["plan_version_id"])
    code, exe, _ = clients.operator.post(f"/v1/publications/{old['publication_id']}/executions", {"mode": "simulated", "idempotency_key": "exe-old"})
    assert code == 201 and exe["publication_id"] == old["publication_id"] and exe["publication_superseded"] is True


def test_tool_and_financemodel_roles_cannot_record_executions(flow: Flow, clients: Any) -> None:
    plan, _snap, root = flow.validated_plan()
    pub = flow.publish(plan["plan_id"], root["plan_version_id"])
    for client in (clients.writer, clients.reader, clients.submitter, clients.fm_job, clients.fm_job_api):
        code, err, _ = client.post(f"/v1/publications/{pub['publication_id']}/executions", {"mode": "paper", "idempotency_key": "exe-t"})
        assert code == 403 and err["code"] == "FORBIDDEN"


def test_unknown_publication_is_not_found(clients: Any) -> None:
    code, err, _ = clients.operator.post("/v1/publications/pub_01KDVDNAZ83BAMMYCEGWF33DPM/executions", {"mode": "paper", "idempotency_key": "exe-nf"})
    assert code == 404 and err["code"] == "NOT_FOUND"
