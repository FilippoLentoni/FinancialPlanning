"""Checksum-bearing reads, download grants and snapshot reads (tasks 4.5, 4.8; API-09, API-12)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import requests
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate

from tests.api_support import *
from tests.api_support import Flow, all_strings, seed_snapshot


def _download(url: str) -> bytes:
    """Fetch a presigned grant (moto intercepts the HTTP call; no network)."""
    resp = requests.get(url, timeout=5)
    assert resp.status_code == 200, resp.status_code
    return resp.content


def _no_locations(doc: Any, buckets: dict[str, str], *, allow_grant_urls: bool = True) -> None:
    """No bucket name or object key in any response field except a presigned grant's ``url``."""
    for ptr, value in all_strings(doc):
        if allow_grant_urls and ptr.endswith("/url") and ("download_grant" in ptr or "download_grants" in ptr):
            continue
        for name in buckets.values():
            assert name not in value, ptr
        assert "content.json" not in value and "manifest.json" not in value and "observations.json" not in value, ptr
        assert not value.startswith("s3://") and "amazonaws.com" not in value, ptr


def _keys(doc: Any) -> set[str]:
    if isinstance(doc, dict):
        return set(doc) | {k for v in doc.values() for k in _keys(v)}
    if isinstance(doc, list):
        return {k for v in doc for k in _keys(v)}
    return set()


# ------------------------------------------------------------------ API-09
def test_plan_artifact_download_hashes_to_the_reference_checksum(flow: Flow, clients: Any, buckets: dict[str, str]) -> None:
    _plan, _snap, root = flow.validated_plan()
    code, got, _ = clients.reader.get(f"/v1/plan-versions/{root['plan_version_id']}?download=true")
    assert code == 200 and validate(got, "tools/get-plan-version-response").valid
    ref, grant = got["content_ref"], got["download_grant"]
    assert validate(ref, "artifact-ref").valid and ref["checksum"] == got["plan_version"]["checksum"]
    assert grant["checksum"] == ref["checksum"] and grant["expires_at"] == "2026-01-12T14:35:00Z"  # 300 s TTL (config)
    data = _download(grant["url"])
    assert "sha256:" + hashlib.sha256(data).hexdigest() == ref["checksum"]
    assert json.loads(data) == got["plan_version"]["content"]
    _no_locations(got, buckets)


def test_snapshot_read_with_download_grants(flow: Flow, clients: Any, svc: Any, clock: Any, buckets: dict[str, str]) -> None:
    sid = seed_snapshot(svc, clock)
    code, got, _ = clients.reader.get(f"/v1/snapshots/{sid}?download=true")
    assert code == 200, got
    assert validate(got["snapshot"], "input-snapshot").valid and got["snapshot"]["status"] == "approved"
    refs = {r["artifact_id"]: r for r in got["snapshot"]["artifacts"]}
    assert {g["artifact_id"] for g in got["download_grants"]} == set(refs)
    for g in got["download_grants"]:
        assert "sha256:" + hashlib.sha256(_download(g["url"])).hexdigest() == refs[g["artifact_id"]]["checksum"]
    _no_locations(got, buckets)


def test_reads_without_grants_expose_no_storage_location(flow: Flow, clients: Any, svc: Any, clock: Any, buckets: dict[str, str]) -> None:
    plan, snap, root = flow.validated_plan()
    pub = flow.publish(plan["plan_id"], root["plan_version_id"])
    for path in (
        f"/v1/plans/{plan['plan_id']}",
        f"/v1/plans/{plan['plan_id']}/versions",
        f"/v1/plan-versions/{root['plan_version_id']}",
        f"/v1/publications/{pub['publication_id']}",
        f"/v1/snapshots/{snap}",
        f"/v1/snapshots/{snap}/observations",
    ):
        code, body, _ = clients.reader.get(path)
        assert code == 200, (path, body)
        _no_locations(body, buckets, allow_grant_urls=False)
        assert not (_keys(body) & {"VersionId", "version_id", "object_version", "s3_version_id", "bucket", "key"})  # STO-05


def test_content_tampering_is_detected_on_read(flow: Flow, clients: Any, svc: Any, ddb: Any) -> None:
    _plan, _snap, root = flow.validated_plan()
    item = ddb.get_item(TableName="finplan-beta-financialplanning-plan-version", Key={"pk": {"S": root["plan_version_id"]}})["Item"]
    doc = json.loads(item["doc"]["S"])
    doc["content"]["fees"]["transaction_cost_bps"] = 999
    item["doc"] = {"S": json.dumps(doc)}
    ddb.put_item(TableName="finplan-beta-financialplanning-plan-version", Item=item)
    code, err, _ = clients.reader.get(f"/v1/plan-versions/{root['plan_version_id']}")
    assert code == 500 and err["code"] == "INTERNAL" and "Traceback" not in json.dumps(err)


# ------------------------------------------------------------------ API-12
def test_observation_read_outside_coverage_states_the_uncovered_range(clients: Any, svc: Any, clock: Any) -> None:
    sid = seed_snapshot(svc, clock)  # coverage 2026-01-02 .. 2026-01-09
    fixture = load_store().fixtures_dir("snapshot-payload") / "valid" / "etf-daily.json"
    payload_dates = {o["session_date"] for o in json.loads(fixture.read_text())["observations"]}
    code, got, _ = clients.reader.get(f"/v1/snapshots/{sid}/observations?instrument_id=SPY&start_date=2025-12-29&end_date=2026-01-14")
    assert code == 200 and validate(got, "tools/query-market-data-response").valid
    assert got["uncovered_ranges"] == [{"start": "2025-12-29", "end": "2026-01-01"}, {"start": "2026-01-10", "end": "2026-01-14"}]
    returned = {o["session_date"] for o in got["observations"]}
    assert returned <= payload_dates  # nothing fabricated
    assert all("2026-01-02" <= d <= "2026-01-09" for d in returned)
    assert got["partial"] is True and got["data_refs"][0]["kind"] == "snapshot_payload"
    for o in got["observations"]:
        assert validate(o, "observation").valid


def _synthetic_payload(dates: list[str]) -> dict[str, Any]:
    base = json.loads((load_store().fixtures_dir("snapshot-payload") / "valid" / "etf-daily.json").read_text())
    template = base["observations"][0]
    base["observations"] = [{**template, "session_date": d, "close": 100.0 + i} for i, d in enumerate(dates)]
    return base


def test_observation_pages_are_bounded_and_stable(clients: Any, svc: Any, clock: Any) -> None:
    dates = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    sid = seed_snapshot(svc, clock, payload=_synthetic_payload(dates))
    _, full, _ = clients.reader.get(f"/v1/snapshots/{sid}/observations")
    seen = []
    token = None
    while True:
        q = "?page_size=2" + (f"&next_token={token}" if token else "")
        code, page, _ = clients.reader.get(f"/v1/snapshots/{sid}/observations{q}")
        assert code == 200 and len(page["observations"]) <= 2
        seen += page["observations"]
        token = page["next_token"]
        if not token:
            break
    assert seen == full["observations"] and [o["session_date"] for o in seen] == dates
    code, err, _ = clients.reader.get(f"/v1/snapshots/{sid}/observations?page_size=3&next_token={token or 'eyJvIjoyLCJmIjoieCJ9'}")
    assert code == 400 and err["code"] == "VALIDATION_FAILED"  # a token from another query is refused


def test_financemodel_job_reads_approved_snapshots_only(clients: Any, svc: Any, clock: Any) -> None:
    approved = seed_snapshot(svc, clock)
    committed = seed_snapshot(svc, clock, status="committed")
    assert clients.fm_job.get(f"/v1/snapshots/{approved}")[0] == 200
    assert clients.fm_job.get(f"/v1/snapshots/{approved}/observations")[0] == 200
    for path in (f"/v1/snapshots/{committed}", f"/v1/snapshots/{committed}/observations"):
        code, err, _ = clients.fm_job.get(path)
        assert code in (403, 422) and err["code"] in ("FORBIDDEN", "PRECONDITION_FAILED")
    assert clients.reader.get(f"/v1/snapshots/{committed}")[0] == 200  # tool readers see committed snapshots


def test_expired_snapshot_reads_as_not_found_naming_the_expiry(clients: Any, svc: Any, clock: Any) -> None:
    sid = seed_snapshot(svc, clock, status="expired", doc_overrides={"status": "expired"})
    code, err, _ = clients.reader.get(f"/v1/snapshots/{sid}")
    assert code == 404 and err["code"] == "NOT_FOUND" and err["details"]["reason"] == "expired"
