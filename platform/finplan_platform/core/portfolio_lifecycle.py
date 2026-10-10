"""Durable issued decisions, confirmed paper fills and immutable holdings history.

S3 contains the exact immutable evidence; DynamoDB is the authoritative commit
index. A resolution, holdings head, history pointer and audit event commit in one
transaction. Uncommitted S3 objects are never exposed by read APIs.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from finplan_contracts.canonical import canonicalize

from .artifacts import sha256_checksum
from .audit import audit_event
from .context import OperationContext
from .contract_io import GET_BY_ID_REQUEST, require_valid
from .errors import PlatformError
from .repository import Check, Cond, HeadMove, IdempotentOutcome, Mutation, PutNew, Transition, decode_page_token
from .services import Services
from .snapshot_reads import load_payloads
from .upgrade import check_declared_version, current_version, serve

_DECISION_RE = re.compile(r"^pd_[0-9A-HJKMNP-TV-Z]{26}$")
MAX_ARTIFACT_BYTES = 512 * 1024


def _check_beta(ctx: OperationContext) -> None:
    if ctx.env != "beta":
        raise PlatformError.not_permitted("paper decision lifecycle is currently enabled only in beta")


def _envelope(**fields: Any) -> dict[str, Any]:
    return {**fields, "synthetic": True, "contract_version": current_version()}


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise PlatformError.validation(f"{field} must be a finite {'positive' if positive else 'nonnegative'} number", pointer=f"/{field}")
    return float(value)


def _revision(value: Any, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PlatformError.validation("revision must be a positive integer", pointer="/expected_revision")
    return value


def _decision_id(value: str) -> None:
    if not isinstance(value, str) or not _DECISION_RE.fullmatch(value):
        raise PlatformError.validation("invalid decision identifier", pointer="/decision_id")


def _write_json(svc: Services, key: str, doc: Mapping[str, Any]) -> str:
    raw = canonicalize(dict(doc))
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise PlatformError.validation("financial evidence exceeds the supported artifact size")
    return svc.store.put_once_or_verify("reports", key, raw).checksum


def _read_json(svc: Services, key: str, checksum: str) -> dict[str, Any]:
    raw, _ = svc.store.get("reports", key, expected_checksum=checksum)
    return json.loads(raw)


def history_id(portfolio_id: str, revision: int) -> str:
    return f"{portfolio_id}:{revision:020d}"


def history_op(ctx: OperationContext, svc: Services, portfolio_id: str, revision: int, state: Mapping[str, Any], *, reason: str, decision_id: str | None = None, recorded_at: str | None = None) -> PutNew:
    """Stage immutable revision evidence; callers commit the pointer with the head."""
    rid = history_id(portfolio_id, revision)
    doc = _envelope(portfolio_id=portfolio_id, revision=revision, paper_state=copy.deepcopy(dict(state)), recorded_at=recorded_at or ctx.now_ts(), reason=reason)
    if decision_id:
        doc["decision_id"] = decision_id
    if state.get("input_snapshot_id"):
        doc["input_snapshot_id"] = state["input_snapshot_id"]
    # A staged artifact must not reserve a revision: a failed concurrent transaction
    # can leave an orphan, so each attempt has its own immutable object key.
    key = f"portfolio-history/{portfolio_id}/{revision:020d}/{ctx.new_id('artifact_id')}.json"
    checksum = _write_json(svc, key, doc)
    return PutNew("portfolio_history", doc={"history_id": rid, "portfolio_id": portfolio_id, "revision": revision, "artifact_key": key, "checksum": checksum}, contract_version=current_version())


def baseline_ops(ctx: OperationContext, svc: Services, portfolio: Any) -> list[PutNew]:
    revision = int(portfolio.attrs.get("paper_state_revision") or 0)
    if revision and portfolio.attrs.get("paper_state") and not svc.repo.get("portfolio_history", history_id(portfolio.id, revision)):
        return [history_op(ctx, svc, portfolio.id, revision, portfolio.attrs["paper_state"], reason="legacy_baseline")]
    return []


def _state(portfolio: Any) -> tuple[int, dict[str, Any]]:
    if portfolio.doc.get("synthetic") is not True:
        raise PlatformError.not_permitted("only synthetic paper portfolios support decision resolution")
    state = portfolio.attrs.get("paper_state")
    revision = int(portfolio.attrs.get("paper_state_revision") or 0)
    if not state or revision < 1:
        raise PlatformError.precondition("saved paper holdings have not been initialized", reason="portfolio_state_missing")
    return revision, copy.deepcopy(state)


def _execution(svc: Services, snapshot: Any, supplied: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(supplied, Mapping) or set(supplied) != {"reference_date", "reference_prices", "target_weights", "transaction_cost_bps"}:
        raise PlatformError.validation("execution requires reference_date, reference_prices, target_weights and transaction_cost_bps", pointer="/execution")
    if snapshot.attrs.get("status") != "approved":
        raise PlatformError.precondition("decision market snapshot must be approved", reason="snapshot_not_approved")
    prices = supplied["reference_prices"]
    weights = supplied["target_weights"]
    if not isinstance(prices, Mapping) or not isinstance(weights, Mapping) or not weights or len(weights) > 100:
        raise PlatformError.validation("execution prices and target weights must be bounded objects", pointer="/execution")
    prices = {str(k): _finite(v, "execution/reference_prices", positive=True) for k, v in prices.items()}
    weights = {str(k): _finite(v, "execution/target_weights") for k, v in weights.items()}
    if abs(sum(weights.values()) - 1.0) > 1e-8:
        raise PlatformError.validation("target weights must sum to one", pointer="/execution/target_weights")
    cost_bps = _finite(supplied["transaction_cost_bps"], "execution/transaction_cost_bps")
    if cost_bps > 1000:
        raise PlatformError.validation("paper costs cannot exceed 1000 basis points", pointer="/execution/transaction_cost_bps")
    reference_date = str(supplied["reference_date"])
    payloads = load_payloads(svc, snapshot)
    observations = [o for p in payloads for o in p.get("observations", []) if o.get("kind") == "completed_daily"]
    if not observations or not any(o["session_date"] == reference_date for o in observations):
        raise PlatformError.precondition("execution must reference a completed session present in the snapshot", reason="reference_date_mismatch")
    actual_prices = {o["instrument_id"]: float(o["close"]) for o in observations if o["session_date"] == reference_date}
    instruments = (set(weights) | {p["instrument_id"] for p in state["positions"]}) - {"USD_CASH"}
    if set(prices) != instruments:
        raise PlatformError.validation("reference prices must cover exactly target and held instruments", pointer="/execution/reference_prices")
    for instrument, price in prices.items():
        if instrument not in actual_prices or not math.isclose(price, actual_prices[instrument], rel_tol=1e-9, abs_tol=1e-8):
            raise PlatformError.validation("reference price differs from the approved snapshot close", pointer="/execution/reference_prices")
    return {"reference_date": reference_date, "reference_prices": prices, "target_weights": weights, "transaction_cost_bps": cost_bps}


def create_decision(ctx: OperationContext, svc: Services, portfolio_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    _check_beta(ctx)
    if ctx.caller.role_class not in ("platform", "operator", "financemodel-job-api"):
        raise PlatformError("FORBIDDEN", "only the model producer may capture issued recommendations")
    require_valid({"portfolio_id": portfolio_id}, GET_BY_ID_REQUEST("portfolio_id"))
    req = copy.deepcopy(dict(request))
    allowed = {"portfolio_id", "algorithm_family", "algorithm", "input_snapshot_id", "portfolio_revision", "recommendation", "provenance", "source_analysis_id", "execution", "idempotency_key", "contract_version", "synthetic"}
    if set(req) - allowed or (req.get("portfolio_id", portfolio_id) != portfolio_id):
        raise PlatformError.validation("unknown or inconsistent decision field")
    req["portfolio_id"] = portfolio_id
    require_valid(req, "api/create-portfolio-decision-request")
    check_declared_version(req.get("contract_version"))
    required = allowed - {"source_analysis_id", "contract_version", "synthetic"}
    if required - req.keys() or req["algorithm_family"] not in ("reinforcement_learning", "optimization") or not isinstance(req["algorithm"], str) or not 1 <= len(req["algorithm"]) <= 128 or not isinstance(req["recommendation"], dict) or not isinstance(req["provenance"], dict):
        raise PlatformError.validation("invalid decision proposal")
    require_valid({"input_snapshot_id": req["input_snapshot_id"]}, GET_BY_ID_REQUEST("input_snapshot_id"))
    revision = _revision(req["portfolio_revision"])

    def execute() -> Mutation:
        portfolio = svc.repo.require("portfolio", portfolio_id)
        current, state = _state(portfolio)
        if revision != current:
            raise PlatformError.conflict("proposal holdings revision is stale", expected_revision=revision, current_revision=current)
        snapshot = svc.repo.require("snapshot_catalog", req["input_snapshot_id"])
        execution = _execution(svc, snapshot, req["execution"], state)
        did = ctx.new_id("decision_id")
        doc = _envelope(decision_id=did, portfolio_id=portfolio_id, algorithm_family=req["algorithm_family"], algorithm=req["algorithm"], input_snapshot_id=req["input_snapshot_id"], portfolio_revision=revision, recommendation=req["recommendation"], provenance=req["provenance"], execution=execution, status="proposed", created_at=ctx.now_ts())
        if req.get("source_analysis_id"):
            doc["source_analysis_id"] = req["source_analysis_id"]
        key = f"portfolio-decisions/{portfolio_id}/{did}/proposal.json"
        checksum = _write_json(svc, key, doc)
        index = {k: doc[k] for k in ("decision_id", "portfolio_id", "algorithm_family", "algorithm", "input_snapshot_id", "portfolio_revision", "status", "created_at")}
        index.update(artifact_key=key, checksum=checksum)
        ops = [Check("portfolio", record_id=portfolio_id, condition=Cond.eq("paper_state_revision", revision), error=lambda _: PlatformError.conflict("holdings changed while capturing decision")), PutNew("portfolio_decision", doc=index, contract_version=current_version()), *baseline_ops(ctx, svc, portfolio)]
        event = audit_event(ctx, record_id=did, record_type="portfolio_decision", operation="create_portfolio_decision", new={"status": "proposed"}, portfolio_id=portfolio_id, input_snapshot_id=req["input_snapshot_id"], portfolio_revision=revision, checksum=checksum)
        return Mutation(ops=ops, response=_envelope(decision={**doc, "checksum": checksum}), audit=[event])
    return svc.repo.run_idempotent(ctx, operation="create_portfolio_decision", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)


def _decision(svc: Services, record: Any) -> dict[str, Any]:
    doc = _read_json(svc, record.doc["artifact_key"], record.doc["checksum"])
    doc["checksum"] = record.doc["checksum"]
    doc["status"] = record.doc["status"]
    if record.doc.get("resolution_key"):
        doc["resolution"] = {**_read_json(svc, record.doc["resolution_key"], record.doc["resolution_checksum"]), "checksum": record.doc["resolution_checksum"]}
    return doc


def get_decision(ctx: OperationContext, svc: Services, decision_id: str) -> dict[str, Any]:
    _check_beta(ctx)
    _decision_id(decision_id)
    return _envelope(decision=_decision(svc, svc.repo.require("portfolio_decision", decision_id)))


def _page(svc: Services, query: Mapping[str, Any], *, partition: str, value: str) -> tuple[int, str | None]:
    limit = query.get("page_size", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= svc.cfg.limits["page_size_max"]:
        raise PlatformError.validation("page_size is out of range", pointer="/page_size")
    token = query.get("next_token")
    if token and (decode_page_token(token).get(partition) or {}).get("S") != value:
        raise PlatformError.validation("next_token belongs to another history", pointer="/next_token")
    return limit, token


def list_decisions(ctx: OperationContext, svc: Services, portfolio_id: str, query: Mapping[str, Any]) -> dict[str, Any]:
    _check_beta(ctx)
    require_valid({"portfolio_id": portfolio_id}, GET_BY_ID_REQUEST("portfolio_id"))
    svc.repo.require("portfolio", portfolio_id)
    limit, token = _page(svc, query, partition="portfolio_id", value=portfolio_id)
    records, token = svc.repo.query_index("portfolio_decision", "portfolio-index", portfolio_id, limit=limit, page_token=token)
    # GSIs are eventually consistent; refresh mutable decision heads with a
    # strongly consistent item read so an indexed proposal cannot hide a resolution.
    return _envelope(portfolio_id=portfolio_id, decisions=[_decision(svc, svc.repo.require("portfolio_decision", r.id)) for r in records], next_token=token)


def _history(svc: Services, record: Any) -> dict[str, Any]:
    return {**_read_json(svc, record.doc["artifact_key"], record.doc["checksum"]), "checksum": record.doc["checksum"]}


def get_history(ctx: OperationContext, svc: Services, portfolio_id: str, revision: str | int) -> dict[str, Any]:
    _check_beta(ctx)
    require_valid({"portfolio_id": portfolio_id}, GET_BY_ID_REQUEST("portfolio_id"))
    if isinstance(revision, str) and re.fullmatch(r"[0-9]{1,10}", revision):
        revision = int(revision)
    revision = _revision(revision)
    return _envelope(history=_history(svc, svc.repo.require("portfolio_history", history_id(portfolio_id, revision))))


def list_history(ctx: OperationContext, svc: Services, portfolio_id: str, query: Mapping[str, Any]) -> dict[str, Any]:
    _check_beta(ctx)
    require_valid({"portfolio_id": portfolio_id}, GET_BY_ID_REQUEST("portfolio_id"))
    portfolio = svc.repo.require("portfolio", portfolio_id)
    baseline = baseline_ops(ctx, svc, portfolio)
    if baseline:
        try:
            svc.repo.commit(baseline)
        except PlatformError as exc:
            if exc.code != "IMMUTABLE_RECORD":
                raise
    limit, token = _page(svc, query, partition="portfolio_id", value=portfolio_id)
    records, token = svc.repo.query_index("portfolio_history", "portfolio-index", portfolio_id, limit=limit, page_token=token)
    return _envelope(portfolio_id=portfolio_id, history=[_history(svc, r) for r in records], next_token=token)


def _paper_fill(before: Mapping[str, Any], execution: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    prices = execution["reference_prices"]
    old = {p["instrument_id"]: float(p["quantity"]) for p in before["positions"]}
    weights = execution["target_weights"]
    value = float(before["cash_balance"]) + sum(q * prices[i] for i, q in old.items())
    if not math.isfinite(value) or value <= 0:
        raise PlatformError.precondition("paper portfolio must have positive finite value")
    rate = execution["transaction_cost_bps"] / 10000.0
    instruments = sorted(set(prices))
    def turnover(v: float) -> float:
        return sum(abs(weights.get(i, 0.0) * v - old.get(i, 0.0) * prices[i]) for i in instruments)
    # Solve post-cost wealth + proportional trading cost = pre-trade wealth.
    # Function is strictly increasing because transaction_cost_bps < 10000.
    lo, hi = 0.0, value
    for _ in range(100):
        mid = (lo + hi) / 2
        if mid + rate * turnover(mid) > value:
            hi = mid
        else:
            lo = mid
    post_value = (lo + hi) / 2
    fills = []
    positions = []
    total_cost = 0.0
    signed_notional = 0.0
    for instrument in instruments:
        quantity = weights.get(instrument, 0.0) * post_value / prices[instrument]
        delta = quantity - old.get(instrument, 0.0)
        if quantity > 1e-12:
            positions.append({"instrument_id": instrument, "quantity": quantity})
        if abs(delta) > 1e-12:
            notional = abs(delta) * prices[instrument]
            fee = notional * rate
            fills.append({"instrument_id": instrument, "side": "buy" if delta > 0 else "sell", "quantity": abs(delta), "reference_price": prices[instrument], "notional": notional, "transaction_cost": fee})
            total_cost += fee
            signed_notional += delta * prices[instrument]
    cash = float(before["cash_balance"]) - signed_notional - total_cost
    tolerance = max(1e-7, value * 1e-10)
    if cash < -tolerance or not math.isclose(cash, weights.get("USD_CASH", 0) * post_value, rel_tol=1e-8, abs_tol=tolerance):
        raise PlatformError.internal("paper fills failed self-financing cash validation")
    after = copy.deepcopy(dict(before))
    after.update(positions=positions, cash_balance=max(0.0, cash), high_watermark=max(float(before["high_watermark"]), value), as_of=execution["reference_date"], source="paper_rebalance")
    observed_after = after["cash_balance"] + sum(p["quantity"] * prices[p["instrument_id"]] for p in positions)
    if not math.isclose(observed_after + total_cost, value, rel_tol=1e-10, abs_tol=tolerance):
        raise PlatformError.internal("paper fills failed wealth conservation")
    return after, fills, total_cost


def resolve_decision(ctx: OperationContext, svc: Services, decision_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    _check_beta(ctx)
    _decision_id(decision_id)
    if ctx.caller.role_class not in ("platform", "operator", "plan-writer"):
        raise PlatformError("FORBIDDEN", "caller cannot resolve paper decisions")
    if ctx.caller.role_class == "plan-writer" and not (ctx.caller.on_behalf_of or {}).get("subject_hash"):
        raise PlatformError("FORBIDDEN", "verified user identity is required for paper decisions")
    req = copy.deepcopy(dict(request))
    allowed = {"decision_id", "action", "expected_revision", "confirmed_by_user", "idempotency_key", "contract_version", "reason", "synthetic"}
    if set(req) - allowed or req.get("action") not in ("accept", "reject") or req.get("confirmed_by_user") is not True:
        raise PlatformError.validation("explicitly confirmed accept or reject is required")
    if req.get("decision_id", decision_id) != decision_id:
        raise PlatformError.validation("decision_id in the body does not match the path")
    req["decision_id"] = decision_id
    expected = _revision(req.get("expected_revision"))
    require_valid(req, "api/resolve-portfolio-decision-request")
    check_declared_version(req.get("contract_version"))

    def execute() -> Mutation:
        record = svc.repo.require("portfolio_decision", decision_id)
        proposal = _decision(svc, record)
        if proposal["status"] != "proposed":
            resolved = proposal["resolution"]
            if resolved["action"] == req["action"] and expected == resolved["before_revision"]:
                # Permanent status, not expiring idempotency, prevents a second fill.
                return Mutation(ops=[], response=resolved)
            raise PlatformError.conflict("decision has already been resolved", status=proposal["status"])
        portfolio_id = proposal["portfolio_id"]
        portfolio = svc.repo.require("portfolio", portfolio_id)
        current, before = _state(portfolio)
        if current != expected or expected != proposal["portfolio_revision"]:
            raise PlatformError.conflict("decision holdings revision is stale; regenerate the recommendation", expected_revision=expected, current_revision=current, proposal_revision=proposal["portfolio_revision"])
        execution = proposal["execution"]
        after, fills, cost = before, [], 0.0
        ops = []
        if req["action"] == "accept":
            snapshot = svc.repo.require("snapshot_catalog", proposal["input_snapshot_id"])
            if snapshot.attrs.get("status") != "approved":
                raise PlatformError.precondition("decision snapshot is no longer approved")
            from .snapshot_reads import latest_approved_snapshot
            latest = latest_approved_snapshot(ctx, svc, {"dataset_id": snapshot.doc["dataset"]["dataset_id"]})
            if latest["snapshot"]["input_snapshot_id"] != proposal["input_snapshot_id"]:
                raise PlatformError.conflict("newer approved market data exists; regenerate the recommendation", reason="stale_market_snapshot")
            completed_dates = [str(o["session_date"]) for payload in load_payloads(svc, snapshot) for o in payload.get("observations", []) if o.get("kind") == "completed_daily"]
            if not completed_dates or execution["reference_date"] != max(completed_dates):
                raise PlatformError.conflict("historical recommendations cannot update current paper holdings; regenerate for the latest session", reason="stale_reference_date")
            ops.append(Check("snapshot_catalog", record_id=snapshot.id, condition=Cond.eq("status", "approved"), error=lambda _: PlatformError.conflict("decision snapshot approval changed during acceptance")))
            after, fills, cost = _paper_fill(before, execution)
            after["input_snapshot_id"] = proposal["input_snapshot_id"]
            require_valid(after, "paper-portfolio-state")
            ops.extend(baseline_ops(ctx, svc, portfolio))
            ops.extend([HeadMove("portfolio", record_id=portfolio_id, expected_revision=expected, revision_attr="paper_state_revision", set_attrs={"paper_state": after}), history_op(ctx, svc, portfolio_id, expected + 1, after, reason="accepted_paper_decision", decision_id=decision_id)])
        else:
            ops.append(Check("portfolio", record_id=portfolio_id, condition=Cond.eq("paper_state_revision", expected), error=lambda _: PlatformError.conflict("holdings changed during rejection")))
        resolution = _envelope(decision_id=decision_id, portfolio_id=portfolio_id, action=req["action"], status="accepted" if req["action"] == "accept" else "rejected", before_revision=expected, after_revision=expected + (req["action"] == "accept"), paper_state=after, simulated_fills=fills, transaction_cost=cost, paper_execution=req["action"] == "accept", recorded_at=ctx.now_ts(), reference_date=execution["reference_date"], reference_prices=execution["reference_prices"], input_snapshot_id=proposal["input_snapshot_id"], confirmed_by_user=True, execution_mode="historical_reference_simulation", caller=ctx.caller.audit_view())
        if req.get("reason"):
            if not isinstance(req["reason"], str) or len(req["reason"]) > 4096:
                raise PlatformError.validation("reason must be a string of at most 4096 characters")
            resolution["reason"] = req["reason"]
        key = f"portfolio-decisions/{portfolio_id}/{decision_id}/resolutions/{ctx.new_id('resolution_id')}.json"
        checksum = _write_json(svc, key, resolution)
        new_doc = {**record.doc, "status": resolution["status"], "resolution_key": key, "resolution_checksum": checksum}
        ops.append(Transition("portfolio_decision", record_id=decision_id, old_doc=record.doc, new_doc=new_doc, error=lambda _: PlatformError.conflict("decision was resolved concurrently")))
        event = audit_event(ctx, record_id=portfolio_id, record_type="portfolio", operation="resolve_portfolio_decision", prior={"revision": expected}, new={"revision": resolution["after_revision"]}, decision_id=decision_id, action=req["action"], input_snapshot_id=proposal["input_snapshot_id"], transaction_cost=cost, resolution_checksum=checksum)
        return Mutation(ops=ops, response={**resolution, "checksum": checksum}, audit=[event])
    return svc.repo.run_idempotent(ctx, operation="resolve_portfolio_decision", idempotency_key=req.get("idempotency_key"), request_body={**req, "decision_id": decision_id}, execute=execute)


_SENSITIVE = {"authorization", "access_token", "refresh_token", "id_token", "password", "secret", "client_secret", "aws_access_key_id", "aws_secret_access_key", "aws_session_token", "api_key", "apikey", "download_grants", "download_grant", "upload", "token", "credentials"}


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _sanitize(v) for k, v in value.items() if str(k).lower() not in _SENSITIVE and not any(s in str(k).lower() for s in ("password", "secret", "authorization"))}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, str):
        if "x-amz-" in value.lower() or value.lower().startswith("bearer "):
            return "[redacted credential or signed grant]"
    return value


def record_activity(ctx: OperationContext, svc: Services, request: Mapping[str, Any]) -> IdempotentOutcome:
    _check_beta(ctx)
    req = copy.deepcopy(dict(request))
    allowed = {"event_kind", "session_id", "correlation_id", "payload", "portfolio_id", "decision_id", "input_snapshot_id", "idempotency_key", "contract_version", "synthetic"}
    if set(req) - allowed or not isinstance(req.get("payload"), dict) or not isinstance(req.get("event_kind"), str) or not 1 <= len(req["event_kind"]) <= 128:
        raise PlatformError.validation("invalid durable activity event")
    if not isinstance(req.get("correlation_id"), str) or req["correlation_id"] != ctx.correlation_id:
        raise PlatformError.validation("activity correlation_id must match authenticated transport")
    require_valid(req, "api/activity-event-request")
    for field in ("portfolio_id", "input_snapshot_id"):
        if field in req:
            require_valid({field: req[field]}, GET_BY_ID_REQUEST(field))
    if "decision_id" in req:
        _decision_id(req["decision_id"])
    if "session_id" in req and (not isinstance(req["session_id"], str) or not 1 <= len(req["session_id"]) <= 256):
        raise PlatformError.validation("invalid activity session_id")
    check_declared_version(req.get("contract_version"))

    def execute() -> Mutation:
        aid = ctx.new_id("activity_event_id")
        doc = {k: _sanitize(v) for k, v in req.items() if k not in ("idempotency_key", "contract_version")}
        doc.update(activity_event_id=aid, recorded_at=ctx.now_ts(), caller=ctx.caller.audit_view(), contract_version=current_version())
        key = f"activity-events/{ctx.now().date().isoformat()}/{aid}.json"
        checksum = _write_json(svc, key, doc)
        index = {k: doc[k] for k in ("activity_event_id", "event_kind", "recorded_at", "portfolio_id", "session_id") if k in doc}
        index.update(artifact_key=key, checksum=checksum)
        response = {"activity_event_id": aid, "recorded_at": doc["recorded_at"], "checksum": checksum, "contract_version": current_version()}
        return Mutation(ops=[PutNew("activity_event", doc=index, contract_version=current_version())], response=response)
    return svc.repo.run_idempotent(ctx, operation="record_activity_event", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)


def list_activity(ctx: OperationContext, svc: Services, query: Mapping[str, Any]) -> dict[str, Any]:
    _check_beta(ctx)
    if bool(query.get("portfolio_id")) == bool(query.get("session_id")):
        raise PlatformError.validation("exactly one portfolio_id or session_id is required")
    field = "portfolio_id" if query.get("portfolio_id") else "session_id"
    value = query[field]
    if field == "portfolio_id":
        require_valid({field: value}, GET_BY_ID_REQUEST(field))
    limit, token = _page(svc, query, partition=field, value=value)
    records, token = svc.repo.query_index("activity_event", "portfolio-index" if field == "portfolio_id" else "session-index", value, limit=limit, page_token=token)
    events = [{**_read_json(svc, r.doc["artifact_key"], r.doc["checksum"]), "checksum": r.doc["checksum"]} for r in records]
    return {"events": events, "next_token": token, "contract_version": current_version()}
