"""Excel export/import through the plan API (tasks 8.1, 8.3, 8.4, 8.5): XLS-01, XLS-04..XLS-06, STO-07.

Offline: the router (:class:`LocalClient`) over the DynamoDB fake and moto S3. The browser's
presigned-POST upload is simulated by a direct put to the granted ``uploads/incoming/`` key.
Workbooks are synthetic and generated at test time.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from finplan_contracts.validate import validate as contract_validate

from finplan_platform.core.artifacts import key_excel_incoming, key_excel_source
from finplan_platform.excel import import_workbook_bytes
from tests.api_support import *  # noqa: F403
from tests.api_support import Flow, content, seed_snapshot
from tests.unit.excel_support import craft, unzip, rezip

DOCS = Path(__file__).resolve().parents[2] / "docs" / "excel.md"
pytestmark = pytest.mark.usefixtures("no_network")


class Excel:
    def __init__(self, flow: Flow, svc: Any, s3: Any, buckets: dict[str, str]) -> None:
        self.flow = flow
        self.svc = svc
        self.s3 = s3
        self.buckets = buckets

    @property
    def web(self) -> Any:
        return self.flow.clients.website

    def export(self, pv: str, *, key: str | None = None) -> tuple[dict[str, Any], bytes]:
        body = self.flow.ok(self.web.post(f"/v1/plan-versions/{pv}/exports", {"idempotency_key": key or self.flow.key("exp")}))
        data, _ = self.svc.store.get("plans", self._export_key(body))
        return body, data

    def _export_key(self, body: dict[str, Any]) -> str:
        from finplan_platform.excel.service import _export_key

        return _export_key(body["plan_id"], body["plan_version_id"], body["export_ref"]["checksum"])

    def grant(self, plan_id: str) -> dict[str, Any]:
        return self.flow.ok(self.web.post(f"/v1/plans/{plan_id}/imports", {"idempotency_key": self.flow.key("grant")}))

    def upload(self, import_id: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.buckets["raw"], Key=key_excel_incoming(import_id), Body=data)

    def commit(self, plan_id: str, import_id: str, *, key: str | None = None, **extra: Any) -> tuple[int, dict[str, Any], dict[str, str]]:
        return self.web.post(f"/v1/plans/{plan_id}/imports/{import_id}/commit", {"idempotency_key": key or self.flow.key("imp"), **extra})

    def import_bytes(self, plan_id: str, data: bytes, *, key: str | None = None, **extra: Any) -> tuple[int, dict[str, Any], dict[str, str]]:
        g = self.grant(plan_id)
        self.upload(g["import_id"], data)
        return self.commit(plan_id, g["import_id"], key=key, **extra)

    def version(self, pv: str) -> dict[str, Any]:
        return self.flow.ok(self.web.get(f"/v1/plan-versions/{pv}"))["plan_version"]


def edit(data: bytes, old: str, new: str) -> bytes:
    """Simulate a user edit of the allocations sheet (a value replaced in place)."""
    parts = unzip(data)
    sheet = parts["xl/worksheets/sheet2.xml"].decode()
    assert old in sheet, old
    parts["xl/worksheets/sheet2.xml"] = sheet.replace(old, new, 1).encode()
    return rezip(parts)


@pytest.fixture
def excel(flow: Flow, svc: Any, s3: Any, buckets: dict[str, str]) -> Excel:
    return Excel(flow, svc, s3, buckets)


@pytest.fixture
def base(flow: Flow) -> dict[str, Any]:
    plan, snap, root = flow.validated_plan()
    return {"plan_id": plan["plan_id"], "snapshot_id": snap, "pv_a": root["plan_version_id"], "checksum": root["checksum"]}


# ===================================================================== XLS-01 export
def test_export_names_version_checksum_and_head_revision_XLS_01(excel: Excel, base: dict[str, Any], flow: Flow) -> None:
    body, data = excel.export(base["pv_a"])
    head = flow.head(base["plan_id"])
    assert body["plan_version_id"] == base["pv_a"] and body["base_checksum"] == base["checksum"]
    assert body["expected_revision"] == head["head"]["revision"]
    grant = body["download_grant"]
    assert grant["url"].startswith("https://") and grant["expires_at"] > body["exported_at"]
    assert grant["checksum"] == "sha256:" + hashlib.sha256(data).hexdigest() == body["export_ref"]["checksum"]
    assert contract_validate(body["export_ref"], "artifact-ref").valid and body["export_ref"]["kind"] == "plan_export"
    parsed = import_workbook_bytes(data, excel.svc.cfg.limits)
    assert parsed.metadata["base_plan_version_id"] == base["pv_a"] and parsed.metadata["base_checksum"] == base["checksum"]
    # re-parsing yields the source content and the checksum matches
    from finplan_platform.core.versions import content_checksum
    from finplan_platform.excel import content_from_workbook

    source = excel.version(base["pv_a"])["content"]
    assert content_checksum(content_from_workbook(source, parsed)) == base["checksum"]
    # nothing in the response but the presigned URL names storage
    without_url = json.dumps({k: v for k, v in body.items() if k != "download_grant"})
    assert "example-beta" not in without_url and "uploads/" not in without_url


def test_export_is_idempotent_and_denied_to_tool_roles(excel: Excel, base: dict[str, Any], clients: Any) -> None:
    first, _ = excel.export(base["pv_a"], key="exp-same")
    code, again, headers = excel.web.post(f"/v1/plan-versions/{base['pv_a']}/exports", {"idempotency_key": "exp-same"})
    assert code == 200 and headers.get("X-Idempotent-Replay") == "true"
    assert again["export_ref"] == first["export_ref"] and again["download_grant"]["url"]
    for cls in ("reader", "writer", "fm_job_api"):
        code, body, _ = getattr(clients, cls).post(f"/v1/plan-versions/{base['pv_a']}/exports", {"idempotency_key": "exp-tool"})
        assert code == 403, body


# ===================================================================== STO-07 / grants
def test_import_grant_is_presigned_post_with_expiry(excel: Excel, base: dict[str, Any], flow: Flow) -> None:
    g = excel.grant(base["plan_id"])
    assert g["import_id"].startswith("imp_") and g["max_bytes"] == excel.svc.cfg.limits["excel_upload_max_bytes"]
    assert g["upload"]["url"].startswith("https://") and "policy" in {k.lower() for k in g["upload"]["fields"]}
    # a replay after the 15-minute expiry is refused (no fresh form for a stale grant)
    key = flow.key("grant-replay")
    flow.ok(excel.web.post(f"/v1/plans/{base['plan_id']}/imports", {"idempotency_key": key}))
    flow.clock.advance(minutes=16)
    code, body, _ = excel.web.post(f"/v1/plans/{base['plan_id']}/imports", {"idempotency_key": key})
    assert code == 422 and body["details"]["reason"] == "upload_grant_expired"


def test_commit_without_upload_or_for_other_plan(excel: Excel, base: dict[str, Any], flow: Flow) -> None:
    g = excel.grant(base["plan_id"])
    code, body, _ = excel.commit(base["plan_id"], g["import_id"])
    assert code == 422 and body["details"]["reason"] == "upload_missing"
    other = flow.plan()
    code, body, _ = excel.commit(other["plan_id"], g["import_id"])
    assert code == 404


# ===================================================================== XLS-04 round trip
def test_excel_override_round_trip_XLS_04(excel: Excel, base: dict[str, Any], flow: Flow, svc: Any) -> None:
    pv_a = base["pv_a"]
    before = excel.version(pv_a)
    _, wb = excel.export(pv_a)
    edited = edit(edit(wb, "<v>0.6</v>", "<v>0.5</v>"), "<v>0.4</v>", "<v>0.5</v>")
    code, report, _ = excel.import_bytes(base["plan_id"], edited)
    assert code == 200, report
    assert report["outcome"] == "accepted" and report["base_plan_version_id"] == pv_a
    assert contract_validate({k: v for k, v in report.items() if k not in ("import_id", "plan_version", "contract_version")}, "import-report").valid
    pv_b = report["created_plan_version_id"]
    b = excel.version(pv_b)
    assert b["parent_plan_version_id"] == pv_a and b["origin"] == "excel_import" and b["run_id"] is None
    assert b["content"]["allocation"] == {"weights": [{"instrument_id": "SPY", "weight": 0.5}], "cash_weight": 0.5}
    assert b["content"]["constraints"] == before["content"]["constraints"] and b["content"]["fees"] == before["content"]["fees"]
    assert excel.version(pv_a) == before  # pv_A unchanged
    assert flow.validate(pv_b)["status"] == "validated"

    # re-export pv_B and re-import it unchanged: a no-effect child with pv_B's checksum
    _, wb_b = excel.export(pv_b)
    code, report2, _ = excel.import_bytes(base["plan_id"], wb_b)
    assert code == 200, report2
    c = excel.version(report2["created_plan_version_id"])
    assert report2["no_effect"] is True and c["no_effect"] is True
    assert c["checksum"] == b["checksum"] and c["parent_plan_version_id"] == pv_b


def test_stale_workbook_conflicts_naming_current_head_XLS_04(excel: Excel, base: dict[str, Any], flow: Flow, svc: Any) -> None:
    _, wb = excel.export(base["pv_a"])
    moved = flow.child(base["plan_id"], base["pv_a"])  # the head moves after the export
    code, body, _ = excel.import_bytes(base["plan_id"], wb)
    assert code == 409 and body["code"] == "CONFLICT"
    assert body["details"]["current_version_id"] == moved["plan_version_id"] and body["details"]["reason"] == "stale_workbook"
    assert flow.head(base["plan_id"])["head"]["current_version_id"] == moved["plan_version_id"]


def test_unknown_instrument_named_by_sheet_and_row_XLS_04(excel: Excel, base: dict[str, Any], svc: Any) -> None:
    _, wb = excel.export(base["pv_a"])
    parts = unzip(wb)
    sheet = parts["xl/worksheets/sheet2.xml"].decode()
    row4 = '<row r="4"><c r="A4" t="inlineStr"><is><t>SYNTH-UNKNOWN</t></is></c><c r="B4"><v>0</v></c></row>'
    parts["xl/worksheets/sheet2.xml"] = sheet.replace("</sheetData>", row4 + "</sheetData>").encode()
    code, body, _ = excel.import_bytes(base["plan_id"], rezip(parts))
    assert code == 400 and body["code"] == "VALIDATION_FAILED"
    assert (body["details"]["sheet"], body["details"]["row"], body["details"]["instrument_id"]) == ("allocations", 4, "SYNTH-UNKNOWN")
    assert sum(1 for r in svc.repo.scan("plan_version") if r.doc.get("origin") == "excel_import") == 0


def test_formula_cell_import_fails_and_creates_nothing_XLS_03(excel: Excel, base: dict[str, Any], svc: Any) -> None:
    _, wb = excel.export(base["pv_a"])
    bad = edit(wb, '<c r="B2"><v>0.6</v></c>', '<c r="B2"><f>0.3*2</f><v>0.6</v></c>')
    code, body, _ = excel.import_bytes(base["plan_id"], bad)
    assert code == 400 and body["details"]["findings"][0]["details"]["cell"] == "B2"
    assert sum(1 for r in svc.repo.scan("plan_version") if r.doc.get("origin") == "excel_import") == 0


def test_macro_workbook_rejected_through_api_XLS_02(excel: Excel, base: dict[str, Any], svc: Any) -> None:
    _, wb = excel.export(base["pv_a"])
    bad = craft(wb, add={"xl/vbaProject.bin": b"synthetic"})
    code, body, _ = excel.import_bytes(base["plan_id"], bad, file_name="plan.xlsx")
    assert code == 400 and body["details"]["reason"] == "macro_content_rejected"
    code, body, _ = excel.import_bytes(base["plan_id"], wb, file_name="plan.xlsm")
    assert code == 400 and body["details"]["reason"] == "unsupported_format"
    assert sum(1 for r in svc.repo.scan("plan_version") if r.doc.get("origin") == "excel_import") == 0


# ===================================================================== XLS-05 lineage
def test_source_file_lineage_XLS_05(excel: Excel, base: dict[str, Any], svc: Any) -> None:
    _, wb = excel.export(base["pv_a"])
    edited = edit(wb, "<v>0.6</v>", "<v>0.55</v>")
    edited = edit(edited, "<v>0.4</v>", "<v>0.45</v>")
    code, report, _ = excel.import_bytes(base["plan_id"], edited)
    assert code == 200, report
    b = excel.version(report["created_plan_version_id"])
    sha = hashlib.sha256(edited).hexdigest()
    assert b["source_artifact"]["kind"] == "excel_source" and b["source_artifact"]["checksum"] == "sha256:" + sha
    assert report["source_artifact"]["checksum"] == "sha256:" + sha
    stored, _ = svc.store.get("raw", key_excel_source(sha))
    assert stored == edited
    assert contract_validate(b, "plan-version").valid


def test_rejected_upload_is_retained_with_rejection_record_XLS_05(excel: Excel, base: dict[str, Any], svc: Any, s3: Any, buckets: dict[str, str]) -> None:
    _, wb = excel.export(base["pv_a"])
    # content-level rejection: base known -> import-report artifact + audit event
    bad = edit(wb, '<c r="B2"><v>0.6</v></c>', '<c r="B2"><f>0.3*2</f><v>0.6</v></c>')
    g = excel.grant(base["plan_id"])
    excel.upload(g["import_id"], bad)
    code, _, _ = excel.commit(base["plan_id"], g["import_id"])
    assert code == 400
    assert svc.store.exists("raw", key_excel_source(hashlib.sha256(bad).hexdigest()))
    events = [e for e in svc.repo.audit_events(g["import_id"]) if e.operation == "reject_excel_import"]
    assert len(events) == 1 and events[0].details["reason"] == "workbook_content_rejected"
    # package-level rejection (the base is unknown): source retained + audit event
    macro = craft(wb, add={"xl/vbaProject.bin": b"synthetic"})
    g2 = excel.grant(base["plan_id"])
    excel.upload(g2["import_id"], macro)
    assert excel.commit(base["plan_id"], g2["import_id"])[0] == 400
    assert svc.store.exists("raw", key_excel_source(hashlib.sha256(macro).hexdigest()))
    assert [e.details["reason"] for e in svc.repo.audit_events(g2["import_id"]) if e.operation == "reject_excel_import"] == ["macro_content_rejected"]
    # an unknown-instrument rejection writes a contract import-report into reports
    parts = unzip(wb)
    sheet = parts["xl/worksheets/sheet2.xml"].decode().replace("</sheetData>", '<row r="4"><c r="A4" t="inlineStr"><is><t>SYNTH-UNKNOWN</t></is></c><c r="B4"><v>0</v></c></row></sheetData>')
    parts["xl/worksheets/sheet2.xml"] = sheet.encode()
    g3 = excel.grant(base["plan_id"])
    excel.upload(g3["import_id"], rezip(parts))
    assert excel.commit(base["plan_id"], g3["import_id"])[0] == 400
    keys = [k for k, _ in svc.store.list_objects("reports", f"{g3['import_id']}/")]
    assert len(keys) == 1
    report = json.loads(svc.store.get("reports", keys[0])[0])
    assert report["outcome"] == "rejected" and contract_validate(report, "import-report").valid
    assert (report["findings"][0]["sheet"], report["findings"][0]["row"], report["findings"][0]["details"]["instrument_id"]) == ("allocations", 4, "SYNTH-UNKNOWN")


# ===================================================================== XLS-06 idempotent import
def test_double_submit_returns_one_child_XLS_06(excel: Excel, base: dict[str, Any], svc: Any) -> None:
    _, wb = excel.export(base["pv_a"])
    edited = edit(edit(wb, "<v>0.6</v>", "<v>0.7</v>"), "<v>0.4</v>", "<v>0.3</v>")
    g = excel.grant(base["plan_id"])
    excel.upload(g["import_id"], edited)
    c1, r1, _ = excel.commit(base["plan_id"], g["import_id"], key="dup-key")
    c2, r2, h2 = excel.commit(base["plan_id"], g["import_id"], key="dup-key")
    assert (c1, c2) == (200, 200) and h2.get("X-Idempotent-Replay") == "true"
    assert r1["created_plan_version_id"] == r2["created_plan_version_id"]
    # the identical file through a second upload grant under the same key: still the original
    c3, r3, _ = excel.import_bytes(base["plan_id"], edited, key="dup-key")
    assert c3 == 200 and r3["created_plan_version_id"] == r1["created_plan_version_id"]
    children = [r for r in svc.repo.scan("plan_version") if r.doc.get("origin") == "excel_import"]
    assert len(children) == 1
    # a different file under the same key is refused
    other = edit(edited, "<v>0.7</v>", "<v>0.65</v>")
    c4, r4, _ = excel.import_bytes(base["plan_id"], edit(other, "<v>0.3</v>", "<v>0.35</v>"), key="dup-key")
    assert c4 == 422 and r4["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_import_routes_denied_to_tool_roles(excel: Excel, base: dict[str, Any], clients: Any) -> None:
    for cls in ("reader", "submitter", "writer", "fm_job", "fm_job_api"):
        code, _, _ = getattr(clients, cls).post(f"/v1/plans/{base['plan_id']}/imports", {"idempotency_key": "tool-grant"})
        assert code == 403


# ===================================================================== 8.5 documented sample workbook
def _documented_sample() -> tuple[dict[str, Any], list[tuple[str, float]]]:
    text = DOCS.read_text()
    block = re.search(r"<!-- sample-allocations -->\s*\n(.*?)\n<!-- /sample-allocations -->", text, re.S)
    assert block, "docs/excel.md lost its sample-allocations block"
    rows = []
    for line in block.group(1).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] not in ("instrument_id", "") and not cells[0].startswith("-"):
            rows.append((cells[0].strip("`"), float(cells[1])))
    weights = [{"instrument_id": i, "weight": w} for i, w in rows if i != "CASH"]
    cash = next(w for i, w in rows if i == "CASH")
    return content(allocation={"weights": weights, "cash_weight": cash}), rows


def test_documented_sample_workbook_imports_XLS_8_5(excel: Excel, flow: Flow, svc: Any) -> None:
    sample, rows = _documented_sample()
    snap = seed_snapshot(svc, flow.clock)
    plan = flow.plan()
    root = flow.root(plan["plan_id"], snap, body_content=sample)
    _, wb = excel.export(root["plan_version_id"])
    parsed = import_workbook_bytes(wb, svc.cfg.limits)
    assert [(r["instrument_id"], r["target_weight"]) for r in parsed.rows] == rows
    assert parsed.logical.get("synthetic") is True
    code, report, _ = excel.import_bytes(plan["plan_id"], wb)
    assert code == 200 and report["outcome"] == "accepted" and report["no_effect"] is True
    from finplan_contracts.leak_scan import scan_text

    assert not scan_text(DOCS.read_text(), str(DOCS))
