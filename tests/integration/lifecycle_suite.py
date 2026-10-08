"""Deployed-environment integration suite: the synthetic phase 1 lifecycle (tasks 11.1-11.3).

Runs in the pipeline's beta (``integration-beta``) and gamma (``gamma``) stage actions, through
:class:`tests.smoke.transport.SigV4Transport` with the stage role's credentials and the endpoint in
``/finplan/<env>/financialplanning/api/plan-endpoint`` (``tests/integration/test_deployed_environment.py``).
The same code runs offline against the deployment double (``tests/unit/ops/test_integration_double.py``),
which proves the requests are valid before they reach a deployed API.

:func:`run_lifecycle` covers, on a fresh synthetic portfolio and plan per run (every idempotency
key carries the run key, so runs never collide and never touch another plan):

* create a synthetic portfolio and plan; trigger a fixture ingestion (``POST /v1/ingestions``) and
  require a ``synthetic: true`` snapshot (phase 1 never calls a real provider);
* root version on that snapshot (API-04, ID-05); the platform mints every ID: a client-supplied
  ``plan_version_id`` is rejected (ID-03);
* duplicate requests (API-08, ID-10): the same key and body replays the original response, the
  same key with another body is ``IDEMPOTENCY_KEY_REUSED``;
* a child override with the parent's content is created with ``no_effect: true`` and the parent's
  checksum; a changed override has ``no_effect: false`` and inherits the parent's lineage (ID-06);
* concurrent overrides on one head revision: exactly one commits, the rest get ``CONFLICT``; a
  stale ``expected_revision`` is ``CONFLICT`` and creates nothing (API-06, ID-11);
* an unvalidated version cannot be published (``PRECONDITION_FAILED``, ID-08); validate, then
  publish the exact validated version; duplicate publish replays; a stale publication revision is
  ``CONFLICT``;
* a paper execution is recorded separately (the publication and version are unchanged, ID-09,
  API-07); its retry replays; ``mode: live`` is ``OPERATION_NOT_PERMITTED``;
* read-back (API-01, API-09): the plan head, the version read, the version list, the publication
  and the execution all name the same ``plan_version_id`` and checksum as the create response, and
  the downloaded content bytes hash to that checksum.

:func:`gamma_isolation_checks` (task 11.3, contract ENV-03) runs in gamma only: the gamma stage
role is denied the prod SSM segment (so it cannot even discover the prod API endpoint), the prod
metadata tables and the prod buckets. All of these are read attempts that are expected to fail.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from finplan_contracts.canonical import canonicalize

from tests.smoke.smoke_suite import SMOKE_CONFIGURATION_ID, SMOKE_MODEL_VERSION, SMOKE_RUN_ID, Transport

__all__ = [
    "BASE_CONTENT",
    "CHANGED_CONTENT",
    "IntegrationError",
    "LifecycleResult",
    "content_checksum",
    "gamma_isolation_checks",
    "run_key_from",
    "run_lifecycle",
    "sequential",
]

#: Synthetic plan content (S&P 500 tracking ETF plus cash); no market value is ever asserted.
BASE_CONTENT: dict[str, Any] = {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.5}], "cash_weight": 0.5},
    "constraints": {"long_only": True, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5},
}
CHANGED_CONTENT: dict[str, Any] = {**copy.deepcopy(BASE_CONTENT), "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.7}], "cash_weight": 0.3}}
#: A well-formed plan version ID the client must not be allowed to supply (ID-03).
FOREIGN_PV_ID = "pv_01KDVDNAZ83BAMMYCEGWF33DPM"

Response = tuple[int, dict[str, Any], dict[str, str]]
Parallel = Callable[[Iterable[Callable[[], Response]]], list[Response]]


class IntegrationError(AssertionError):
    pass


@dataclass
class LifecycleResult:
    portfolio_id: str = ""
    plan_id: str = ""
    input_snapshot_id: str = ""
    root_id: str = ""
    no_effect_id: str = ""
    plan_version_id: str = ""
    checksum: str = ""
    publication_id: str = ""
    execution_id: str = ""
    steps: list[str] = field(default_factory=list)


def content_checksum(content: Any) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(content)).hexdigest()


def run_key_from(*parts: str | None) -> str:
    """An idempotency-key-safe run key (``[A-Za-z0-9_-]``, at most 64 characters)."""
    text = "-".join(p for p in parts if p)
    return re.sub(r"[^A-Za-z0-9_-]", "-", text)[:64] or "run"


def sequential(calls: Iterable[Callable[[], Response]]) -> list[Response]:
    """Run "concurrent" requests one after the other (the in-process double)."""
    return [c() for c in calls]


def _header(headers: dict[str, str], name: str) -> str | None:
    low = name.lower()
    return next((v for k, v in headers.items() if k.lower() == low), None)


def _expect(resp: Response, status: tuple[int, ...], what: str) -> dict[str, Any]:
    code, body, _ = resp
    if code not in status:
        raise IntegrationError(f"{what}: HTTP {code}: {body.get('code')} {body.get('message')}")
    return body


def _expect_error(resp: Response, status: int, code: str, what: str) -> dict[str, Any]:
    got, body, _ = resp
    if got != status or body.get("code") != code:
        raise IntegrationError(f"{what}: expected HTTP {status} {code}, got HTTP {got} {body.get('code')} {body.get('message')}")
    for key in ("message", "retryable", "correlation_id"):
        if key not in body:
            raise IntegrationError(f"{what}: error envelope lacks {key!r}")
    return body


def _check(cond: bool, message: str) -> None:
    if not cond:
        raise IntegrationError(message)


def run_lifecycle(
    t: Transport,
    *,
    run_key: str,
    dataset_id: str,
    today: date,
    parallel: Parallel = sequential,
    log: Callable[[str], None] = print,
    phase: int = 1,
) -> LifecycleResult:
    """One end-to-end lifecycle run on fresh synthetic records (see module docstring)."""
    r = LifecycleResult()
    key = lambda name: f"it-{name}-{run_key}"  # noqa: E731 - unique per run and step

    def step(text: str) -> None:
        r.steps.append(text)
        log(f"integration: {text}")

    # 1. synthetic portfolio and plan (fresh per run)
    pf = _expect(t.call("POST", "/v1/portfolios", {"name": f"integration {run_key} (synthetic)", "base_currency": "USD", "synthetic": True, "idempotency_key": key("portfolio")}), (200, 201), "create portfolio")
    _check(pf.get("synthetic") is True, "the integration portfolio is not synthetic")
    r.portfolio_id = pf["portfolio_id"]
    plan = _expect(t.call("POST", f"/v1/portfolios/{r.portfolio_id}/plans", {"name": f"integration {run_key}", "expected_revision": pf["revision"], "idempotency_key": key("plan")}), (200, 201), "create plan")
    r.plan_id = plan["plan_id"]
    _check(r.portfolio_id.startswith("pf_") and r.plan_id.startswith("pl_"), "platform IDs carry their contract prefixes")
    step(f"portfolio {r.portfolio_id}, plan {r.plan_id} (synthetic)")

    # 2. fixture snapshot through on-demand ingestion
    start, end = (today - timedelta(days=10)).isoformat(), (today - timedelta(days=1)).isoformat()
    ing = _expect(t.call("POST", "/v1/ingestions", {"dataset_id": dataset_id, "start_date": start, "end_date": end, "granularity": "daily", "idempotency_key": key("ingest")}), (200, 201), "on-demand ingestion")
    sid = ing.get("input_snapshot_id")
    _check(bool(sid), f"ingestion returned no snapshot for {start}..{end} (quality flags {ing.get('quality_flags')})")
    snap = _expect(t.call("GET", f"/v1/snapshots/{sid}"), (200,), "read snapshot")
    snap_doc = snap.get("snapshot", snap)
    if phase == 1:
        _check(snap_doc.get("synthetic") is True, "phase 1 integration requires a synthetic (fixture) snapshot")
    else:  # phase 2: the environment loads real provider data; the plan records stay synthetic
        _check(bool(snap_doc.get("input_snapshot_id")), "phase 2 integration needs a committed snapshot")
    _check(snap_doc.get("input_snapshot_id") == sid, "snapshot read returns another snapshot")
    r.input_snapshot_id = sid
    step(f"snapshot {sid} ({snap_doc.get('status')})")

    # 3. root version on the snapshot; the platform mints IDs (ID-03)
    head = _expect(t.call("GET", f"/v1/plans/{r.plan_id}"), (200,), "read plan head")["plan"]
    root_body = {
        "expected_revision": head["head"]["revision"],
        "idempotency_key": key("root"),
        "domain": "finance",
        "domain_schema_version": "1.0",
        "content": copy.deepcopy(BASE_CONTENT),
        "input_snapshot_id": sid,
        "configuration_id": SMOKE_CONFIGURATION_ID,
        "model_version": SMOKE_MODEL_VERSION,
        "run_id": SMOKE_RUN_ID,
    }
    _expect_error(t.call("POST", f"/v1/plans/{r.plan_id}/versions", {**root_body, "idempotency_key": key("root-minted"), "plan_version_id": FOREIGN_PV_ID}), 400, "VALIDATION_FAILED", "client-supplied plan_version_id")
    code, root, hdrs = t.call("POST", f"/v1/plans/{r.plan_id}/versions", root_body)
    _check(code == 201, f"create root version: HTTP {code}: {root.get('code')} {root.get('message')}")
    base_sum = content_checksum(BASE_CONTENT)
    _check(root.get("checksum") == base_sum, f"root checksum {root.get('checksum')} != JCS SHA-256 of the content {base_sum}")
    _check(root.get("origin") == "model_run" and root.get("parent_plan_version_id") is None and root.get("no_effect") is False, "root version lineage")
    _check(root["plan_version_id"] != FOREIGN_PV_ID and root["plan_version_id"].startswith("pv_"), "root version ID is platform-minted")
    r.root_id = root["plan_version_id"]
    step(f"root version {r.root_id}")

    # 4. duplicate requests (API-08, ID-10)
    code, again, hdrs = t.call("POST", f"/v1/plans/{r.plan_id}/versions", root_body)
    _check(code == 201 and again.get("plan_version_id") == r.root_id and again.get("checksum") == base_sum, "an idempotent retry must return the original version")
    _check((_header(hdrs, "X-Idempotent-Replay") or "").lower() == "true", "an idempotent retry is marked as a replay")
    _expect_error(t.call("POST", f"/v1/plans/{r.plan_id}/versions", {**root_body, "content": copy.deepcopy(CHANGED_CONTENT)}), 422, "IDEMPOTENCY_KEY_REUSED", "same key, different body")
    head = _expect(t.call("GET", f"/v1/plans/{r.plan_id}"), (200,), "read plan head")["plan"]
    _check(head["head"]["current_version_id"] == r.root_id and head["head"]["revision"] == root["revision"], "a replay must not move the plan head")
    step("idempotent retry returned the original; key reuse rejected")

    # 5. no-effect override (same content, other key order) -> child with the parent's checksum (ID-06)
    def child_body(parent: str, content: dict[str, Any], rev: int, name: str) -> dict[str, Any]:
        return {"parent_plan_version_id": parent, "expected_revision": rev, "idempotency_key": key(name), "domain": "finance", "domain_schema_version": "1.0", "content": content, "reason": "synthetic integration override"}

    reordered = json.loads(json.dumps(BASE_CONTENT, sort_keys=True))
    ne = _expect(t.call("POST", f"/v1/plans/{r.plan_id}/versions", child_body(r.root_id, reordered, head["head"]["revision"], "noeffect")), (201,), "no-effect override")
    _check(ne.get("no_effect") is True and ne.get("checksum") == base_sum, "an override equal to its parent is no_effect with the parent's checksum")
    _check(ne.get("origin") == "manual_override" and ne.get("parent_plan_version_id") == r.root_id and ne["plan_version_id"] != r.root_id, "the no-effect override is still a new child")
    r.no_effect_id = ne["plan_version_id"]
    step(f"no-effect override {r.no_effect_id}")

    # 6. concurrent overrides on one head revision (API-06, ID-11): exactly one commits
    rev = int(ne["revision"])
    racers = [child_body(r.no_effect_id, copy.deepcopy(CHANGED_CONTENT), rev, f"race{i}") for i in range(2)]
    results = parallel([lambda b=b: t.call("POST", f"/v1/plans/{r.plan_id}/versions", b) for b in racers])
    won = [b for c, b, _ in results if c == 201]
    lost = [b for c, b, _ in results if c != 201]
    _check(len(won) == 1, f"exactly one concurrent override commits (got {[c for c, _, _ in results]})")
    _check(all(b.get("code") == "CONFLICT" for b in lost), f"the losing override gets CONFLICT (got {[b.get('code') for b in lost]})")
    child = won[0]
    _check(child.get("no_effect") is False and child.get("checksum") == content_checksum(CHANGED_CONTENT), "a changed override is not no_effect")
    _check(child.get("input_snapshot_id") == sid, "an override inherits its parent's snapshot")
    r.plan_version_id, r.checksum = child["plan_version_id"], child["checksum"]

    # 7. stale expected_revision -> CONFLICT, nothing created
    before = _expect(t.call("GET", f"/v1/plans/{r.plan_id}/versions"), (200,), "list versions")
    _expect_error(t.call("POST", f"/v1/plans/{r.plan_id}/versions", child_body(r.plan_version_id, copy.deepcopy(BASE_CONTENT), rev, "stale")), 409, "CONFLICT", "stale expected_revision")
    after = _expect(t.call("GET", f"/v1/plans/{r.plan_id}/versions"), (200,), "list versions")
    _check(len(after["versions"]) == len(before["versions"]) == 3, f"a CONFLICT creates no version ({len(before['versions'])} -> {len(after['versions'])}, expected 3)")
    step(f"override {r.plan_version_id} won the race; stale revision got CONFLICT")

    # 8. publication needs a validated version (ID-08), then validate and publish
    head = _expect(t.call("GET", f"/v1/plans/{r.plan_id}"), (200,), "read plan head")["plan"]
    pub_rev = head["publication_revision"]
    _expect_error(t.call("POST", f"/v1/plans/{r.plan_id}/publications", {"plan_version_id": r.plan_version_id, "expected_revision": pub_rev, "idempotency_key": key("pub-early")}), 422, "PRECONDITION_FAILED", "publish an unvalidated version")
    val_body = {"idempotency_key": key("validate")}
    val = _expect(t.call("POST", f"/v1/plan-versions/{r.plan_version_id}/validate", val_body), (200,), "validate")
    _check(val.get("status") == "validated", f"validation result {val.get('status')}: {val.get('findings')}")
    _check(_expect(t.call("POST", f"/v1/plan-versions/{r.plan_version_id}/validate", val_body), (200,), "validate retry") == val, "a validate retry returns the original result")
    pub_body = {"plan_version_id": r.plan_version_id, "expected_revision": pub_rev, "idempotency_key": key("publish")}
    pub = _expect(t.call("POST", f"/v1/plans/{r.plan_id}/publications", pub_body), (201,), "publish")
    _check(pub.get("plan_version_id") == r.plan_version_id and pub.get("plan_version_checksum") == r.checksum, "the publication names the exact validated version and its checksum")
    r.publication_id = pub["publication_id"]
    again = _expect(t.call("POST", f"/v1/plans/{r.plan_id}/publications", pub_body), (201,), "publish retry")
    _check(again.get("publication_id") == r.publication_id, "a publish retry returns the original publication")
    _expect_error(t.call("POST", f"/v1/plans/{r.plan_id}/publications", {**pub_body, "idempotency_key": key("publish-stale")}), 409, "CONFLICT", "stale publication revision")
    step(f"validated and published {r.publication_id}")

    # 9. paper execution, recorded separately (ID-09, API-07); live is refused
    pub_before = _expect(t.call("GET", f"/v1/publications/{r.publication_id}"), (200,), "read publication")
    _expect_error(t.call("POST", f"/v1/publications/{r.publication_id}/executions", {"mode": "live", "idempotency_key": key("live")}), 403, "OPERATION_NOT_PERMITTED", "live execution")
    exe_body = {"mode": "paper", "idempotency_key": key("execution")}
    exe = _expect(t.call("POST", f"/v1/publications/{r.publication_id}/executions", exe_body), (201,), "record paper execution")
    _check(exe.get("mode") == "paper" and exe.get("publication_id") == r.publication_id and exe.get("plan_version_id") == r.plan_version_id, "the execution references the publication in paper mode")
    _check(exe.get("synthetic") is True, "the execution record is synthetic")
    r.execution_id = exe["execution_id"]
    again = _expect(t.call("POST", f"/v1/publications/{r.publication_id}/executions", exe_body), (201,), "execution retry")
    _check(again.get("execution_id") == r.execution_id, "an execution retry returns the original execution")
    pub_after = _expect(t.call("GET", f"/v1/publications/{r.publication_id}"), (200,), "read publication")
    _check(pub_after == pub_before, "recording an execution changed the publication")
    step(f"paper execution {r.execution_id}; live refused")

    # 10. read-back: every read names the created version and checksum (API-01, API-09)
    head_doc = _expect(t.call("GET", f"/v1/plans/{r.plan_id}"), (200,), "read plan")
    _check(head_doc["plan"]["head"]["current_version_id"] == r.plan_version_id, "the plan head is the published override")
    _check((head_doc.get("current_publication") or {}).get("publication_id") == r.publication_id, "the plan's current publication")
    got = _expect(t.call("GET", f"/v1/plan-versions/{r.plan_version_id}?download=true"), (200,), "read version")
    pv = got["plan_version"]
    _check(pv["plan_version_id"] == r.plan_version_id and pv["checksum"] == r.checksum and got["content_ref"]["checksum"] == r.checksum, "the direct read returns the created plan_version_id and checksum")
    _check(pv["status"] == "validated" and pv["parent_plan_version_id"] == r.no_effect_id and pv["input_snapshot_id"] == sid, "version status and lineage")
    data = t.download(got["download_grant"]["url"])
    _check("sha256:" + hashlib.sha256(data).hexdigest() == r.checksum, "downloaded content does not hash to the version checksum")
    listed = {v["plan_version_id"]: v["checksum"] for v in _expect(t.call("GET", f"/v1/plans/{r.plan_id}/versions"), (200,), "list versions")["versions"]}
    _check(listed == {r.root_id: base_sum, r.no_effect_id: base_sum, r.plan_version_id: r.checksum}, "the version list matches the created versions and checksums")
    root_read = _expect(t.call("GET", f"/v1/plan-versions/{r.root_id}"), (200,), "read root")["plan_version"]
    _check(root_read["checksum"] == base_sum and root_read["status"] == "pending_validation", "overrides leave the parent unchanged")
    exe_read = _expect(t.call("GET", f"/v1/executions/{r.execution_id}"), (200,), "read execution")
    _check(exe_read.get("plan_version_id") == r.plan_version_id and exe_read.get("publication_id") == r.publication_id, "the execution read names the published version")
    step(f"read-back verified: {r.plan_version_id} {r.checksum}")
    return r


# ===================================================================== gamma isolation
def _denied(call: Callable[[], Any], what: str) -> str:
    """Run a read that must be refused; return the error code."""
    try:
        call()
    except Exception as exc:  # botocore ClientError
        err = getattr(exc, "response", {}).get("Error", {})
        code = str(err.get("Code", ""))
        if code in ("AccessDenied", "AccessDeniedException", "403", "Forbidden"):
            return code
        raise IntegrationError(f"{what}: expected an access denial, got {code or type(exc).__name__}") from None
    raise IntegrationError(f"{what}: the read succeeded; environment isolation is broken")


def gamma_isolation_checks(session: Any, *, env: str = "gamma", other: str = "prod", repo: str = "financialplanning") -> list[str]:
    """The ``env`` stage role is denied ``other``'s SSM segment, tables and buckets (ENV-03)."""
    from tests.smoke.transport import endpoint_parameter

    ssm = session.client("ssm")
    ddb = session.client("dynamodb")
    s3 = session.client("s3")
    account = session.client("sts").get_caller_identity()["Account"]
    out = [
        _denied(lambda: ssm.get_parameter(Name=endpoint_parameter(other)), f"{env} reads the {other} plan endpoint"),
        _denied(lambda: ddb.describe_table(TableName=f"finplan-{other}-{repo}-plan"), f"{env} describes the {other} plan table"),
        _denied(lambda: ddb.get_item(TableName=f"finplan-{other}-{repo}-plan-version", Key={"pk": {"S": "pv_01KDVDNAZ83BAMMYCEGWF33DPM"}}), f"{env} reads the {other} plan-version table"),
        _denied(lambda: s3.head_bucket(Bucket=f"finplan-{other}-{repo}-plans-{account}"), f"{env} reaches the {other} plans bucket"),
    ]
    # the own environment stays reachable (the denials are not a blanket failure)
    ssm.get_parameter(Name=endpoint_parameter(env))
    return out
