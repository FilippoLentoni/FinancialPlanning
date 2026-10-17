"""Bounded evidence aggregation and immutable, self-contained modeling reports.

There are no demo inputs, provider calls, model calls or feature reconstruction here.
Every downloadable input is the original captured inference-boundary bytes. Missing
or invalid evidence is a visible coverage failure, never an invented zero return.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..core.errors import PlatformError

EASTERN = ZoneInfo("America/New_York")
REPORT_VERSION = "modeling-report/1"
BAD_STATUSES = {"failed", "unavailable", "skipped", "blocked", "running", "claimed", "pending"}
SECRET_KEYS = {"password", "secret_access_key", "aws_secret_access_key", "access_token", "api_key", "authorization", "session_token", "aws_session_token"}


class EvidenceError(ValueError):
    """Stored evidence cannot safely be represented as a completed evaluation."""


class EvidenceReader(Protocol):
    def query_day(self, experiment_id: str, day: str, *, kind: str, page_size: int = 100, next_token: str | None = None) -> tuple[list[dict], str | None]: ...
    def read_input(self, input_id: str) -> dict: ...


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EvidenceError("Evidence timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _safe_content(value: Any) -> None:
    if isinstance(value, dict):
        if SECRET_KEYS.intersection(str(k).lower() for k in value):
            raise EvidenceError("Secret-bearing evidence refused; repair source capture")
        for child in value.values():
            _safe_content(child)
    elif isinstance(value, list):
        for child in value:
            _safe_content(child)
    elif isinstance(value, float) and not math.isfinite(value):
        raise EvidenceError("Non-finite financial value")
    elif isinstance(value, str) and ("X-Amz-Signature=" in value or "-----BEGIN PRIVATE KEY" in value):
        raise EvidenceError("Credential-bearing evidence refused")


def _unique(records: list[dict], key: str) -> list[dict]:
    result: dict[str, dict] = {}
    for record in records:
        record_id = str(record.get(key, ""))
        if not record_id:
            raise EvidenceError(f"Missing {key}")
        if record_id in result and canonical(result[record_id]) != canonical(record):
            raise EvidenceError(f"Conflicting duplicate {key}: {record_id}")
        result[record_id] = record
    return list(result.values())


def _pages(reader: EvidenceReader, experiment: str, day: str, kind: str, *, max_records: int) -> list[dict]:
    token = None
    seen: set[str] = set()
    rows: list[dict] = []
    while True:
        page, token = reader.query_day(experiment, day, kind=kind, page_size=100, next_token=token)
        rows.extend(page)
        if len(rows) > max_records:
            raise EvidenceError(f"Report bound exceeded for {kind}/{day}; use a narrower experiment")
        if token is None:
            return rows
        if token in seen:
            raise EvidenceError("Query pagination repeated a continuation token")
        seen.add(token)


def _actual_bytes(captured: dict) -> bytes:
    # A reader may provide original raw bytes, otherwise its canonical bytes must
    # reproduce the persisted checksum. Never pretty-print to create a download.
    raw = captured.get("raw_bytes")
    if raw is not None:
        if not isinstance(raw, bytes):
            raise EvidenceError("Input raw_bytes must be bytes")
        return raw
    return canonical(captured["payload"])


def _input_for_run(reader: EvidenceReader, run: dict) -> dict:
    input_id = run.get("input_id")
    if not input_id:
        raise EvidenceError("Run has no captured model-specific input_id")
    captured = reader.read_input(input_id)
    metadata, payload = captured["metadata"], captured["payload"]
    raw = reader.read_input_bytes(input_id) if hasattr(reader, "read_input_bytes") else _actual_bytes(captured)
    expected = metadata.get("checksum") or metadata.get("payload_checksum") or metadata.get("artifact_checksum")
    if checksum(raw) != expected:
        raise EvidenceError("Captured input checksum does not match original bytes")
    if json.loads(raw) != payload:
        raise EvidenceError("Input payload differs from captured bytes")
    for field in ("input_id", "approach_id", "run_id"):
        if field != "run_id" and metadata.get(field) != run.get(field):
            raise EvidenceError(f"Model-specific input {field} does not match selected run")
        if field in payload and payload[field] != run.get(field):
            raise EvidenceError(f"Payload {field} does not match selected run")
    if metadata.get("slot") and timestamp(metadata["slot"]) != timestamp(run["scheduled_slot"]):
        raise EvidenceError("Captured input belongs to a different scheduled slot")
    if metadata.get("kind", "model_input") != "model_input":
        raise EvidenceError("Shared source bundle substituted for model-specific input")
    # The scheduled opportunity and actual inference cutoff are distinct. A run
    # at :05 must show its captured :05 payload, not falsely backdate it to :00.
    run["decision_time"] = payload.get("decision_time", run["decision_time"])
    run["availability_cutoff"] = payload.get("availability_cutoff", metadata.get("availability_cutoff", run.get("availability_cutoff", run["decision_time"])))
    cutoff = timestamp(run.get("availability_cutoff") or run["decision_time"])
    if timestamp(run["decision_time"]) < cutoff:
        raise EvidenceError("Availability cutoff follows decision time")
    # Only consumed values must precede the cutoff. Explicit excluded source
    # evidence remains inspectable and is not falsely attributed to inference.
    def check_available(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("consumed") is False or value.get("eligible") is False:
                return
            for key in ("available_at", "platform_available_at", "availability_time", "availability_at"):
                if value.get(key) and timestamp(value[key]) > cutoff:
                    raise EvidenceError("Future-available evidence in captured model input")
            for child in value.values():
                check_available(child)
        elif isinstance(value, list):
            for child in value:
                check_available(child)
    check_available(payload)
    _safe_content(payload)
    news = payload.get("news") or {}
    if isinstance(news, dict) and news.get("consumed") is False and news.get("prepared_context"):
        raise EvidenceError("Prepared news context falsely labeled News not consumed")
    return {"metadata": metadata, "payload": payload, "checksum": expected,
            "raw_base64": base64.b64encode(raw).decode(), "size_bytes": len(raw)}


def _value(obs: dict) -> float | None:
    raw = obs.get("value", obs.get("equity", obs.get("portfolio_value")))
    return float(raw) if raw is not None else None


def _valid(obs: dict) -> bool:
    return (_value(obs) is not None and obs.get("status", "valid") in {"valid", "success", "observed", "valued", "market_closed_cash"}
            and obs.get("quote_status", "valid") in {"valid", "fresh", "not_required", "cash_only"}
            and obs.get("observation_kind", "valid") not in {"stale", "carried_forward", "pending", "missing"})


def forecast_metrics(forecasts: list[dict], observations: list[dict], cutoff: str) -> list[dict]:
    """Read-only alignment; persisted forecast content is never rewritten.

    The first VALID same-book observation at/after target is selected. Corporate
    actions and cash flows must already be reflected in authoritative equity.
    A run with a deterministic projection must label forecast_kind accordingly.
    """
    results = []
    for forecast in _unique(forecasts, "forecast_id"):
        row = dict(forecast)
        if forecast.get("comparison_eligible") is False:
            row.update(outcome_status="interim_not_applied", realized_value=None, reason="Interim research does not trade or enter primary forecast rankings")
        elif forecast.get("expected_value") is None:
            row.update(outcome_status="unavailable", realized_value=None)
        elif timestamp(forecast["target_at"]) > timestamp(cutoff):
            row.update(outcome_status="pending", realized_value=None, next_eligible_observation=forecast["target_at"])
        else:
            eligible = [o for o in observations if o.get("portfolio_id") == forecast.get("portfolio_id") and _valid(o)
                        and timestamp(forecast["target_at"]) <= timestamp(o["observed_at"]) <= timestamp(cutoff)]
            eligible.sort(key=lambda o: (timestamp(o["observed_at"]), o["observation_id"]))
            if eligible:
                matched = eligible[0]
                flows = float(matched.get("net_external_cash_flows_since_decision", 0))
                adjusted = _value(matched) - flows
                error = adjusted - float(forecast["expected_value"])
                row.update(outcome_status="matched", realized_value=_value(matched), flow_adjusted_realized_value=adjusted, net_external_cash_flows=flows, signed_error=error,
                           absolute_error=abs(error), matched_observation_id=matched["observation_id"],
                           observed_at=matched["observed_at"], quote_provenance=matched.get("quote_provenance"))
            else:
                row.update(outcome_status="pending_market_data", realized_value=None, next_eligible_observation=forecast["target_at"])
        results.append(row)
    return results



def _session_proof(value: Any) -> tuple[set[str], bool]:
    """Only persisted session/quote evidence establishes observed market sessions."""
    dates, proven = set(), False
    if isinstance(value, dict):
        session = value.get("market_session")
        if isinstance(session, dict) and session.get("source") and session.get("status") in {"open", "closed"}:
            proven = True
            if session["status"] == "open" and session.get("session_date"):
                dates.add(session["session_date"])
        regular = value.get("regular_session") or {}
        if isinstance(value.get("market_open"), bool) and regular.get("start") and regular.get("end"):
            start, end = timestamp(regular["start"]), timestamp(regular["end"])
            if end > start:
                if value["market_open"] is False:
                    proven = True
                elif value.get("quoted_at") and start <= timestamp(value["quoted_at"]) <= end:
                    proven = True
                    dates.add(start.astimezone(EASTERN).date().isoformat())
        for child in value.values():
            child_dates, child_proven = _session_proof(child)
            dates.update(child_dates); proven = proven or child_proven
    elif isinstance(value, list):
        for child in value:
            child_dates, child_proven = _session_proof(child)
            dates.update(child_dates); proven = proven or child_proven
    return dates, proven


def _compact_session_quotes(value: dict) -> list[dict]:
    quotes = (value.get("current_quotes") or {}).get("quotes", [])
    if isinstance(quotes, dict):
        quotes = list(quotes.values())
    keys = ("instrument_id", "market_open", "regular_session", "quoted_at", "available_at", "retrieved_at", "status", "provider", "source_response_checksum")
    return [{k: q[k] for k in keys if k in q} for q in quotes if isinstance(q, dict)]


def summarize(approaches: list[dict], runs: list[dict], observations: list[dict], forecasts: list[dict], execution_events: list[dict] | None = None) -> list[dict]:
    books = []
    for approach in approaches:
        if approach.get("non_trading"):
            continue
        aid = approach["approach_id"]
        selected = [r for r in runs if r["approach_id"] == aid]
        executions = [e for e in execution_events if e["approach_id"] == aid] if execution_events is not None else None
        if executions is None:
            execution_cost = sum(float(r["execution_cost"]) for r in selected) if selected and all(r.get("execution_cost") is not None for r in selected) else None
            fills_count = sum(len(r.get("fills", [])) for r in selected)
            turnover = sum(float(r.get("turnover", 0) or 0) for r in selected)
        else:
            execution_cost = sum(float(e["transaction_cost"]) for e in executions) if all(e.get("transaction_cost") is not None for e in executions) else None
            fills_count = sum(len(e.get("simulated_fills", [])) for e in executions)
            turnover = sum(float(e.get("execution", {}).get("turnover", 0) or 0) for e in executions)
        history = sorted([o for o in observations if o.get("approach_id") == aid], key=lambda o: (timestamp(o["observed_at"]), o["observation_id"]))
        valid = [o for o in history if _valid(o)]
        opening, current = (_value(valid[0]), _value(valid[-1])) if valid else (None, None)
        # Net external flows are excluded from investment P&L. Prefer the common
        # evaluator's persisted time-weighted return when cash flows occurred.
        executed_notional = None
        if executions is not None:
            executed_notional = sum(abs(float(fill["notional"])) for event in executions for fill in event.get("simulated_fills", []))
            turnover = executed_notional / opening if opening else None
        flows = sum(float(o.get("external_cash_flow", 0)) for o in valid[1:])
        pnl = current - opening - flows if valid else None
        ret = pnl / opening if opening else None
        if flows:
            ret = valid[-1].get("time_weighted_return")
        peak = opening or 0
        drawdown = 0.0
        for obs in valid:
            value = _value(obs)
            peak = max(peak, value)
            drawdown = max(drawdown, (peak - value) / peak if peak else 0)
        matched = [f for f in forecasts if f.get("approach_id") == aid and f.get("outcome_status") == "matched" and f.get("forecast_kind", "model_forecast") == "model_forecast"]
        failures = sum(r.get("status") in BAD_STATUSES or bool(r.get("evidence_error")) for r in selected)
        observed_dates = {timestamp(o["observed_at"]).astimezone(EASTERN).date().isoformat() for o in valid}
        session_dates, session_proof_count = set(), 0
        for observation in valid:
            proven_dates, proof = _session_proof({"quote_provenance": observation.get("quote_provenance"),
                                                 "market_session": observation.get("market_session"),
                                                 "market_session_evidence": observation.get("market_session_evidence")})
            session_dates.update(proven_dates)
            session_proof_count += int(proof)
        observation_key = tuple(o["observed_at"] for o in valid)
        policy = approach.get("comparison_policy_id")
        cohort = approach.get("cohort_id")
        reasons = []
        if executions is not None and any(e.get("evidence_error") for e in executions):
            reasons.append("Missing/corrupt execution-day receipt")
        if len(valid) < 2:
            reasons.append("Fewer than two valid valuations")
        if failures or not selected:
            reasons.append("Missing, failed or unavailable approach evaluations")
        if any(not _valid(o) for o in history):
            reasons.append("Missing/stale valuation slots")
        if ret is None:
            reasons.append("Comparable return unavailable")
        if not policy or not cohort:
            reasons.append("Comparison policy/cohort not declared")
        books.append({"approach_id": aid, "display_name": approach.get("display_name", aid),
                      "portfolio_id": approach.get("portfolio_id"), "family": approach.get("family"),
                      "opening_value": opening, "current_value": current, "net_pnl": pnl, "net_return": ret,
                      "max_drawdown": drawdown if valid else None,
                      "turnover": turnover, "executed_notional": executed_notional,
                      "turnover_definition": "absolute_executed_notional_divided_by_period_opening_value" if executions is not None else "legacy_sum_of_supplied_turnover",
                      "execution_cost": execution_cost,
                      "model_cost": sum(float(r["model_cost"]) for r in selected) if selected and all(r.get("model_cost") is not None for r in selected) else None,
                      "model_cost_basis": sorted({r.get("model_cost_basis", "unavailable") for r in selected}),
                      "recommendations": len(selected), "fills": fills_count,
                      "execution_event_ids": [e["observation_id"] for e in executions] if executions is not None else [],
                      "execution_accounting": "actual_execution_day_events" if executions is not None else "legacy_issue_day_fallback",
                      "valuations": len(history), "valid_valuations": len(valid), "failure_rate": failures / len(selected) if selected else None,
                      "forecast_matches": len(matched), "mae": sum(f["absolute_error"] for f in matched) / len(matched) if matched else None,
                      "rmse": math.sqrt(sum(f["signed_error"] ** 2 for f in matched) / len(matched)) if matched else None,
                      "coverage": len(valid) / len(history) if history else 0,
                      "observed_dates": len(observed_dates), "observed_sessions": len(session_dates) if session_proof_count else None,
                      "market_session_dates": sorted(session_dates), "market_session_proof_coverage": session_proof_count/len(valid) if valid else None,
                      "window_start": valid[0]["observed_at"] if valid else None, "window_end": valid[-1]["observed_at"] if valid else None,
                      "comparison_policy_id": policy, "cohort_id": cohort, "observation_window": list(observation_key),
                      "ranking_blockers": reasons, "rank": None, "return_tied": False})
    # Rank only within an identical declared cohort, capital, policy and valuation
    # window; missing models do not get a fabricated zero or an equivalent rank.
    groups: dict[tuple, list[dict]] = {}
    by_id = {a["approach_id"]: a for a in approaches}
    for book in books:
        if not book["ranking_blockers"]:
            approach = by_id[book["approach_id"]]
            key = (book["cohort_id"], book["comparison_policy_id"], tuple(book["observation_window"]),
                   canonical(approach.get("investable_universe", [])).decode(), approach.get("starting_capital"))
            groups.setdefault(key, []).append(book)
    for members in groups.values():
        if len(members) < 2:
            members[0]["ranking_blockers"].append("No second approach with identical comparable window")
            continue
        members.sort(key=lambda b: (-b["net_return"], b["max_drawdown"], b["approach_id"]))
        for index, book in enumerate(members, 1):
            book["rank"] = index
            book["return_tied"] = any(other is not book and math.isclose(other["net_return"], book["net_return"], rel_tol=1e-10, abs_tol=1e-12) for other in members)
    return sorted(books, key=lambda b: (b["rank"] is None, b["rank"] or 0, b["approach_id"]))


def aggregate(reader: EvidenceReader, *, experiment_id: str, registry: list[dict], start_day: str, end_day: str | None = None,
              generated_at: str | None = None, finalized: bool = False, mode: str = "forward", max_records: int = 20000,
              comparison_policy: dict | None = None) -> dict:
    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day or start_day)
    if end < start or (end - start).days > 6:
        raise EvidenceError("Reports are bounded to one through seven calendar days")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    timestamp(generated_at)
    comparison_policy = comparison_policy or {}
    registry = [{"comparison_policy_id": comparison_policy.get("comparison_policy_id", comparison_policy.get("policy_id")),
                 "cohort_id": comparison_policy.get("cohort_id"),
                 "starting_capital": comparison_policy.get("starting_capital", comparison_policy.get("initial_capital")),
                 "investable_universe": comparison_policy.get("investable_universe", comparison_policy.get("universe")), **row}
                for row in _unique(registry, "approach_id")]
    if not registry:
        raise EvidenceError("A persisted approach registry is required")
    portfolio_ids = [r.get("portfolio_id") for r in registry]
    if None in portfolio_ids or len(set(portfolio_ids)) != len(portfolio_ids):
        raise EvidenceError("Every approach must have its own isolated portfolio_id")
    days = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days + 1)]
    runs, observations = [], []
    for day in days:
        runs.extend(_pages(reader, experiment_id, day, "runs", max_records=max_records))
        observations.extend(_pages(reader, experiment_id, day, "observations", max_records=max_records))
    runs = _unique(runs, "run_id")
    observations = _unique(observations, "observation_id")
    # Normalize authoritative storage field names without reconstructing inputs.
    for run in runs:
        run.setdefault("scheduled_slot", run.get("slot"))
        run["scheduled_slot"] = timestamp(run["scheduled_slot"]).isoformat()
        run.setdefault("decision_time", run.get("started_at", run.get("slot")))
        run.setdefault("availability_cutoff", run.get("slot", run["decision_time"]))
        if run.get("outcome") is not None:
            run["output"] = run["outcome"]
        execution = run.get("execution") or {}
        if execution:
            details = execution.get("execution", {})
            run.update(fills=execution.get("simulated_fills", []),
                       fill_status=execution.get("execution_status", execution.get("status")), fill_time=details.get("execution_at"),
                       execution_cost=details.get("transaction_cost"), turnover=details.get("turnover"),
                       decision_id=execution.get("decision_id"))
    for observation in observations:
        payload = observation.get("payload") or {}
        # Envelope identities always win over decoded values.
        for key, value in payload.items():
            if key not in {"experiment_id", "approach_id", "portfolio_id", "observation_id", "observed_at"}:
                observation[key] = value
    inputs: dict[str, dict] = {}
    additional_inputs: dict[str, list[dict]] = {}
    known = {r["approach_id"]: r for r in registry}
    slots: set[tuple[str, str]] = set()
    forecasts = []
    shared_session_cache: dict[str, list[dict]] = {}
    for run in runs:
        if run.get("experiment_id") != experiment_id or run.get("approach_id") not in known:
            raise EvidenceError("Run outside selected experiment/registry")
        aid = run["approach_id"]
        if run.get("portfolio_id") != known[aid]["portfolio_id"]:
            raise EvidenceError("Run points to another approach's portfolio")
        slot_key = (aid, run["scheduled_slot"])
        if slot_key in slots:
            raise EvidenceError("Multiple run IDs for the same permanent approach/slot")
        slots.add(slot_key)
        if timestamp(run["decision_time"]) > timestamp(generated_at):
            raise EvidenceError("Decision follows report data cutoff")
        try:
            inputs[run["run_id"]] = _input_for_run(reader, run)
            captured = inputs[run["run_id"]]
            if known[aid].get("non_trading"):
                run.update(non_trading=True, purpose="interim_research_no_primary_execution",
                           reviewed_portfolio_id=captured["payload"].get("holdings", {}).get("portfolio_id"))
            source_ref = captured["metadata"].get("shared_source_ref")
            if source_ref and source_ref["input_id"] not in shared_session_cache:
                source = reader.read_input(source_ref["input_id"])
                original = reader.read_input_bytes(source_ref["input_id"]) if hasattr(reader, "read_input_bytes") else _actual_bytes(source)
                if source["metadata"].get("kind") != "shared_source" or checksum(original) != source_ref["checksum"] or source["metadata"]["checksum"] != source_ref["checksum"]:
                    raise EvidenceError("Shared valuation/session source checksum or kind mismatch")
                shared_session_cache[source_ref["input_id"]] = _compact_session_quotes(source["payload"])
            if source_ref:
                run["market_session_evidence"] = {"role": "valuation/session context only; not an inference input", "source_input_id": source_ref["input_id"],
                    "source_checksum": source_ref["checksum"], "quotes": shared_session_cache[source_ref["input_id"]]}
        except (EvidenceError, PlatformError, KeyError, ValueError) as error:
            run["evidence_error"] = str(error)
        if timestamp(run["decision_time"]) > timestamp(generated_at):
            raise EvidenceError("Captured actual decision follows report cutoff")
        if hasattr(reader, "read_output") and run.get("output_checksum"):
            try:
                output = reader.read_output(run["run_id"])
                raw = output.get("raw_bytes") or canonical(output["payload"])
                if checksum(raw) != run["output_checksum"] or json.loads(raw) != output["payload"]:
                    raise EvidenceError("Output checksum mismatch")
                run["output"] = output["payload"]
            except (EvidenceError, PlatformError, KeyError, ValueError) as error:
                run["evidence_error"] = str(error)
        output = run.get("output", {})
        if isinstance(output, dict):
            for field in ("target_weights", "target_cash", "intents", "rationale", "citations", "fills", "execution_cost", "turnover", "model_cost", "model_cost_basis", "cash_weight", "forecast", "signal_status", "execution_status", "market_input_status", "price_as_of", "news_research_status", "native_input_status", "native_input_detail"):
                if field in output:
                    run[field] = output[field]
        graph_ref = output.get("graph_sub_boundary_ref", output.get("captured_inputs_ref")) if isinstance(output, dict) else None
        if graph_ref:
            try:
                raw = reader.read_artifact_bytes(graph_ref)
                if checksum(raw) != graph_ref["checksum"]:
                    raise EvidenceError("Graph sub-boundary checksum mismatch")
                payload = json.loads(raw)
                for field in ("run_id", "approach_id", "input_id"):
                    if payload.get(field) != run.get(field):
                        raise EvidenceError(f"Graph sub-boundary {field} mismatch")
                _safe_content(payload)
                additional_inputs[run["run_id"]] = [{"kind": "graph_sub_boundary_inputs", "metadata": graph_ref,
                    "payload": payload, "checksum": graph_ref["checksum"], "size_bytes": len(raw),
                    "raw_base64": base64.b64encode(raw).decode()}]
            except (EvidenceError, PlatformError, KeyError, ValueError) as error:
                run["evidence_error"] = str(error)
        if output.get("usage") and run.get("model_cost") is None:
            run["model_cost"] = output["usage"].get("cost_usd")
            run["model_cost_basis"] = output["usage"].get("cost_basis", "observed" if run["model_cost"] is not None else "unavailable")
        if (run.get("forecast") or {}).get("expected_value") is not None:
            forecast = dict(run["forecast"], approach_id=aid, portfolio_id=run["portfolio_id"], run_id=run["run_id"])
            if known[aid].get("non_trading"):
                forecast.update(portfolio_id=run["forecast"].get("portfolio_id", run.get("reviewed_portfolio_id")), comparison_eligible=False)
            forecast.setdefault("forecast_id", forecast.get("forecast_checksum"))
            if not forecast.get("forecast_id") or not forecast.get("target_at"):
                run["evidence_error"] = "Supplied forecast lacks immutable forecast_id/target_at"
            else:
                forecasts.append(forecast)
    for observation in observations:
        source_run = next((r for r in runs if r["run_id"] == observation.get("run_id")), None)
        if source_run and source_run.get("market_session_evidence"):
            observation["market_session_evidence"] = source_run["market_session_evidence"]
        if observation.get("experiment_id") != experiment_id or observation.get("approach_id") not in known:
            raise EvidenceError("Observation outside selected experiment/registry")
        if observation.get("portfolio_id") != known[observation["approach_id"]]["portfolio_id"]:
            raise EvidenceError("Observation points to another approach's portfolio")
        if timestamp(observation["observed_at"]) > timestamp(generated_at):
            raise EvidenceError("Observation follows report cutoff")
    ancillary = [o for o in observations if o.get("kind", "valuation") not in {"valuation", "opening", "baseline"}]
    observations = [o for o in observations if o.get("kind", "valuation") in {"valuation", "opening", "baseline"}]
    execution_events = [o for o in ancillary if o.get("kind") == "paper_execution"] if getattr(reader, "execution_events_supported", False) else None
    if execution_events is not None:
        event_runs = set()
        for event in execution_events:
            if event.get("evidence_error"):
                event.update(transaction_cost=None, simulated_fills=[], execution={})
                continue
            receipt = event.get("payload") or {}
            if receipt.get("run_id") != event.get("run_id") or receipt.get("portfolio_id") != event["portfolio_id"] or receipt.get("decision_id") != event.get("decision_id"):
                raise EvidenceError("Execution-day event does not match immutable ledger receipt")
            if event["run_id"] in event_runs:
                raise EvidenceError("Multiple execution-day events for one permanent run")
            event_runs.add(event["run_id"])
            event_at = receipt.get("execution", {}).get("execution_at") or receipt["recorded_at"]
            if timestamp(event_at) != timestamp(event["observed_at"]):
                raise EvidenceError("Execution event is indexed on a different execution time")
            event["transaction_cost"] = receipt["transaction_cost"]
            event["simulated_fills"] = receipt["simulated_fills"]
            event["execution"] = receipt["execution"]
            if event["execution"].get("turnover") is None:
                capital = known[event["approach_id"]].get("starting_capital")
                if capital:
                    event["execution"] = {**event["execution"], "turnover": sum(abs(float(f["notional"])) for f in event["simulated_fills"])/float(capital),
                                          "turnover_transformation": "sum_absolute_executed_notional_divided_by_common_starting_capital"}
        for run in runs:
            if run.get("execution_ref") and run.get("fill_time") and timestamp(run["fill_time"]).astimezone(EASTERN).date().isoformat() in days and run["run_id"] not in event_runs:
                run["evidence_error"] = "Immutable actual-execution-day ledger event missing"
    forecasts = forecast_metrics(forecasts, observations, generated_at)
    for forecast in forecasts:
        source_run = next(r for r in runs if r["run_id"] == forecast["run_id"])
        persisted = source_run.get("forecast_realization")
        if persisted:
            if persisted.get("forecast_checksum") != forecast.get("forecast_checksum") or persisted.get("run_id") != forecast["run_id"] or persisted.get("portfolio_id") != forecast["portfolio_id"]:
                raise EvidenceError("Stored realization does not match frozen forecast/run/book")
            if timestamp(persisted["observed_at"]) < timestamp(forecast["target_at"]) or timestamp(persisted["observed_at"]) > timestamp(generated_at):
                raise EvidenceError("Stored realization falls outside horizon/report cutoff")
            if forecast.get("matched_observation_id") and forecast["matched_observation_id"] != persisted["observation_id"]:
                raise EvidenceError("Stored realization violates first-valid-observation alignment")
            forecast.update(outcome_status="matched", realized_value=persisted["realized_value"],
                            signed_error=persisted["signed_error"], absolute_error=persisted["absolute_error"],
                            matched_observation_id=persisted["observation_id"], observed_at=persisted["observed_at"],
                            quote_provenance=persisted.get("quote_provenance"), stored_realization=persisted,
                            stored_realization_ref=source_run.get("forecast_realization_ref", source_run.get("realization_ref")))
        matches = [o for o in ancillary if o.get("kind") == "forecast_match" and o.get("run_id") == forecast["run_id"]]
        if matches:
            if len(matches) != 1:
                raise EvidenceError("Multiple immutable matches for one forecast")
            stored = matches[0]
            if forecast.get("forecast_checksum") and stored.get("forecast_checksum") != forecast["forecast_checksum"]:
                raise EvidenceError("Persisted forecast match checksum mismatch")
            if forecast.get("matched_observation_id") and stored.get("payload", {}).get("observation_id") != forecast["matched_observation_id"]:
                raise EvidenceError("Persisted forecast alignment differs from report valuation")
            forecast["stored_realization_observation_id"] = stored["observation_id"]
            forecast["stored_realization"] = stored.get("payload", {})
    missing = [{"approach_id": aid, "scheduled_slot": slot, "status": "missing_slot"}
               for slot in sorted({r["scheduled_slot"] for r in runs}) for aid in known if (aid, slot) not in slots]
    # Expected hourly opportunities are defined by the persisted experiment policy,
    # rather than trusting that at least one strategy wrote in every hour.
    expected = set()
    first_slot = comparison_policy.get("first_scheduled_slot", comparison_policy.get("scheduled_start_at"))
    if first_slot:
        step = int(comparison_policy.get("cadence_seconds", 3600))
        if step != 3600:
            raise EvidenceError("Primary report requires declared common hourly cadence")
        current = timestamp(first_slot)
        lower = datetime.combine(start, time(), EASTERN).astimezone(timezone.utc)
        upper = min(timestamp(generated_at) - timedelta(seconds=int(comparison_policy.get("decision_offset_seconds", 0))), datetime.combine(end + timedelta(days=1), time(), EASTERN).astimezone(timezone.utc) - timedelta(microseconds=1))
        if current < lower:
            current += timedelta(seconds=math.ceil((lower-current).total_seconds()/step)*step)
        while current <= upper:
            expected.add(current.isoformat())
            current += timedelta(seconds=step)
    elif runs:
        # Incomplete initialization metadata is disclosed. Internal missing hours
        # can still be found without claiming coverage before the first known run.
        current = min(timestamp(r["scheduled_slot"]) for r in runs)
        last = max(timestamp(r["scheduled_slot"]) for r in runs)
        while current <= last:
            expected.add(current.isoformat())
            current += timedelta(hours=1)
    existing = {(aid, timestamp(slot)) for aid, slot in slots}
    for slot in sorted(expected):
        for aid in known:
            if (aid, timestamp(slot)) not in existing and not any(m["approach_id"] == aid and timestamp(m["scheduled_slot"]) == timestamp(slot) for m in missing):
                missing.append({"approach_id": aid, "scheduled_slot": slot, "status": "missing_slot"})
    # A reader may also expose a durable explicit expected-slot manifest.
    if hasattr(reader, "expected_slots"):
        for slot in reader.expected_slots(experiment_id, start_day, end_day or start_day):
            for aid in known:
                if (aid, slot) not in slots and not any(m["approach_id"] == aid and m["scheduled_slot"] == slot for m in missing):
                    missing.append({"approach_id": aid, "scheduled_slot": slot, "status": "missing_slot"})
    for run in runs:
        before = sorted([o for o in observations if o["portfolio_id"] == run["portfolio_id"] and _valid(o)
                         and timestamp(o["observed_at"]) <= timestamp(run["decision_time"])
                         and timestamp(o["observed_at"]).astimezone(EASTERN).date() == timestamp(run["decision_time"]).astimezone(EASTERN).date()],
                        key=lambda o: (timestamp(o["observed_at"]), o["observation_id"]))
        run.update(portfolio_value_at_decision=None, daily_pnl=None, daily_return=None,
                   opening_valuation_id=None, valuation_id=None)
        if before:
            opening, current = before[0], before[-1]
            flows = sum(float(o.get("external_cash_flow", 0)) for o in before[1:])
            pnl = _value(current) - _value(opening) - flows
            run.update(portfolio_value_at_decision=_value(current), daily_pnl=pnl,
                       daily_return=current.get("time_weighted_return") if flows else (pnl/_value(opening) if _value(opening) else None),
                       opening_valuation_id=opening["observation_id"], valuation_id=current["observation_id"])
        forecast = next((f for f in forecasts if f["run_id"] == run["run_id"]), None)
        run.update(forecast_outcome=forecast, expected_value=forecast.get("expected_value") if forecast else None,
                   forecast_target=forecast.get("target_at") if forecast else None,
                   forecast_status=forecast.get("outcome_status") if forecast else "unavailable",
                   realized_value=forecast.get("realized_value") if forecast else None,
                   signed_error=forecast.get("signed_error") if forecast else None)
    leaderboard = summarize(registry, runs, observations, forecasts, execution_events)
    for missing_row in missing:
        book = next((b for b in leaderboard if b["approach_id"] == missing_row["approach_id"]), None)
        if book is None:
            continue
        if "Missing scheduled slots" not in book["ranking_blockers"]:
            book["ranking_blockers"].append("Missing scheduled slots")
        book["rank"] = None
    latest = max([r["decision_time"] for r in runs] + [o["observed_at"] for o in observations + ancillary]
                 + [r[k] for r in runs for k in ("finished_at", "fill_time") if r.get(k)]
                 + [r["execution"]["recorded_at"] for r in runs if (r.get("execution") or {}).get("recorded_at")]
                 + [f["observed_at"] for f in forecasts if f.get("observed_at")], default=None, key=lambda x: timestamp(x) if x else datetime.min.replace(tzinfo=timezone.utc))
    next_eod = datetime.combine(timestamp(generated_at).astimezone(EASTERN).date(), time(17), EASTERN)
    if next_eod <= timestamp(generated_at):
        next_eod += timedelta(days=1)
    result = {"report_version": REPORT_VERSION, "experiment_id": experiment_id, "mode": mode,
              "start_day": start_day, "end_day": end_day or start_day, "timezone": "America/New_York",
              "generated_at": generated_at, "data_cutoff": latest, "finalized": finalized,
              "period": "weekly" if len(days) == 7 else "daily", "window_complete": len(days) == 7 and finalized and not missing,
              "comparison_policy": comparison_policy, "expected_slot_manifest_available": bool(first_slot),
              "renderer_checksum": checksum((Path(__file__).parent / "template.html").read_bytes() + (Path(__file__).parent / "report.js").read_bytes()),
              "ranking_rule": "Net realized return after common execution costs; then lower drawdown; then approach ID. Identical policy/universe/capital/observation windows only. Observed winner is not universal optimality.",
              "registry": registry, "runs": sorted(runs, key=lambda r: (r["decision_time"], r["approach_id"])),
              "inputs": inputs, "additional_inputs": additional_inputs, "observations": observations, "ancillary_evidence": ancillary, "execution_events": execution_events, "forecasts": forecasts,
              "leaderboard": leaderboard, "missing_slots": missing,
              "evidence_failures": [{"run_id": r["run_id"], "error": r["evidence_error"]} for r in runs if r.get("evidence_error")],
              "next_daily_generation": next_eod.isoformat(),
              "source_record_ids": {"runs": [r["run_id"] for r in runs], "observations": [o["observation_id"] for o in observations + ancillary]}}
    _safe_content(result)
    return result




def aggregate_interim(reader: EvidenceReader, *, primary_experiment: str, primary_registry: dict, start_day: str,
                      end_day: str, generated_at: str, finalized: bool = False, mode: str = "forward") -> dict | None:
    """Read actual interim evidence without creating a registry/book/model payload.

    The registry annotation is a report read model derived from persisted bounded
    runs + verified existing evidence book and the primary descriptive reference.
    """
    experiment = primary_experiment + "-interim"
    first, last = date.fromisoformat(start_day), date.fromisoformat(end_day)
    actual = []
    for offset in range((last-first).days+1):
        actual.extend(_pages(reader, experiment, (first+timedelta(days=offset)).isoformat(), "runs", max_records=20000))
    if not actual:
        return None
    approaches = []
    for aid in sorted({r["approach_id"] for r in actual}):
        records = [r for r in actual if r["approach_id"] == aid]
        books = {r["portfolio_id"] for r in records}
        if len(books) != 1:
            raise EvidenceError("Interim approach does not have one permanent evidence book")
        book_id = next(iter(books))
        book = reader.get_portfolio(book_id)
        if book["experiment_id"] != experiment or book["approach_id"] != aid:
            raise EvidenceError("Interim evidence book ownership mismatch")
        reference = next((a for a in primary_registry["approaches"] if a["approach_id"] == aid), None)
        if reference is None:
            raise EvidenceError("Interim approach missing primary descriptive reference")
        approaches.append({**reference, "portfolio_id": book_id, "non_trading": True, "cohort_id": "interim_research_nontrading",
                           "comparison_policy_id": None, "native_cadence": "thirty_minute_interim",
                           "display_name": reference.get("display_name", aid) + " · 30-minute interim research (nontrading)",
                           "registry_annotation_source": "bounded actual runs, existing isolated evidence book, primary registry descriptive reference"})
    report = aggregate(reader, experiment_id=experiment, registry=approaches, start_day=start_day, end_day=end_day,
                       generated_at=generated_at, finalized=finalized, mode=mode)
    report["interim_research"] = True
    return report


def consolidate(reports: list[dict]) -> dict:
    """Combine isolated experiment cohorts without claiming cross-universe parity.

    Captured inputs are validated by aggregate BEFORE display IDs are namespaced.
    Original run/input/book/checksum/approach identities remain available verbatim.
    """
    if not reports or len(reports) > 8:
        raise EvidenceError("Consolidation requires one through eight bounded experiments")
    if len({r["experiment_id"] for r in reports}) != len(reports):
        raise EvidenceError("Duplicate experiment in consolidated report")
    keys = ("start_day", "end_day", "mode", "generated_at", "period", "timezone")
    if any(any(r[k] != reports[0][k] for k in keys) for r in reports):
        raise EvidenceError("Consolidated cohorts must share reporting window/cutoff/mode")
    if len(reports) == 1:
        return reports[0]
    combined = dict(reports[0])
    combined.update(registry=[], runs=[], observations=[], forecasts=[], missing_slots=[], evidence_failures=[],
                    inputs={}, additional_inputs={}, ancillary_evidence=[], execution_events=[] if all(r.get("execution_events") is not None for r in reports) else None, source_record_ids={"runs": [], "observations": []},
                    experiments=[{"experiment_id": r["experiment_id"], "comparison_policy": r["comparison_policy"],
                                  "expected_slot_manifest_available": r["expected_slot_manifest_available"]} for r in reports])
    book_ids, run_ids = set(), set()
    for report in reports:
        experiment = report["experiment_id"]
        for key in ("registry", "runs", "observations", "forecasts", "missing_slots"):
            for original in report[key]:
                row = dict(original)
                aid = row["approach_id"]
                row.update(source_approach_id=aid, approach_id=f"{experiment}::{aid}", experiment_id=experiment)
                if key == "registry":
                    if row["portfolio_id"] in book_ids:
                        raise EvidenceError("Consolidated approaches share a portfolio")
                    book_ids.add(row["portfolio_id"])
                    row["display_name"] = f"{row.get('display_name', aid)} [{row.get('cohort_id') or experiment}]"
                if key == "runs":
                    if row["run_id"] in run_ids:
                        raise EvidenceError("Run IDs overlap consolidated experiments")
                    run_ids.add(row["run_id"])
                combined[key].append(row)
        combined["inputs"].update(report["inputs"])
        combined["additional_inputs"].update(report.get("additional_inputs", {}))
        combined["evidence_failures"].extend(report["evidence_failures"])
        combined["ancillary_evidence"].extend(report.get("ancillary_evidence", []))
        if combined["execution_events"] is not None:
            combined["execution_events"].extend({**e, "source_approach_id": e["approach_id"], "approach_id": f"{experiment}::{e['approach_id']}"} for e in report["execution_events"])
        for key in ("runs", "observations"):
            combined["source_record_ids"][key].extend(report["source_record_ids"][key])
    combined["leaderboard"] = summarize(combined["registry"], combined["runs"], combined["observations"], combined["forecasts"], combined["execution_events"])
    for missing in combined["missing_slots"]:
        book = next((b for b in combined["leaderboard"] if b["approach_id"] == missing["approach_id"]), None)
        if book is None:
            continue
        if "Missing scheduled slots" not in book["ranking_blockers"]:
            book["ranking_blockers"].append("Missing scheduled slots")
        book["rank"] = None
    cutoffs = [r["data_cutoff"] for r in reports if r["data_cutoff"]]
    combined["data_cutoff"] = max(cutoffs, key=timestamp) if cutoffs else None
    combined["window_complete"] = all(r["window_complete"] for r in reports)
    combined["expected_slot_manifest_available"] = all(r["expected_slot_manifest_available"] for r in reports)
    combined["finalized"] = all(r["finalized"] for r in reports)
    return combined


def csv_export(report: dict) -> str:
    stream = io.StringIO(newline="")
    fields = ["experiment_id", "source_approach_id", "run_id", "approach_id", "portfolio_id", "decision_time", "scheduled_slot", "input_id", "availability_cutoff", "status", "target_weights", "target_cash", "rationale", "forecast", "execution_cost", "model_cost", "fills", "portfolio_value_at_decision", "daily_pnl", "daily_return", "valuation_id", "opening_valuation_id", "expected_value", "forecast_target", "forecast_status", "realized_value", "signed_error", "evidence_error"]
    writer = csv.DictWriter(stream, fields, extrasaction="ignore")
    writer.writeheader()
    for run in report["runs"]:
        row = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in run.items() if k in fields}
        # Spreadsheet formulas in untrusted model/news text are not executable.
        row = {k: "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v for k, v in row.items()}
        writer.writerow(row)
    for missing in report["missing_slots"]:
        writer.writerow(missing)
    return stream.getvalue()


def html_export(report: dict) -> str:
    root = Path(__file__).parent
    data = canonical(report).decode().replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return (root / "template.html").read_text().replace("__REPORT_DATA__", data).replace("__REPORT_SCRIPT__", (root / "report.js").read_text())


def write_report(report: dict, output_dir: Path, *, artifact_store: Any = None) -> dict:
    """Atomic mutable local aliases + immutable checksum-addressed revisions.

    S3 reports are written through the platform's existing write-once reports role.
    Every revision includes a manifest of checksum/source IDs; archives are never
    silently overwritten when late observations arrive.
    """
    if report["mode"] not in {"forward", "controlled_replay"}:
        raise EvidenceError("Report mode must explicitly distinguish forward/replay")
    target = output_dir / ("replay" if report["mode"] == "controlled_replay" else "")
    if report["period"] == "weekly":
        target /= "weekly"
    target.mkdir(parents=True, exist_ok=True)
    json_bytes = canonical(report)
    rendered_html = html_export(report).encode()
    revision = checksum(json_bytes + rendered_html).split(":")[1][:20]
    period_name = report["end_day"] if report["period"] == "daily" else f'{report["start_day"]}_{report["end_day"]}'
    safe_experiment = re.sub(r"[^A-Za-z0-9_.-]", "_", report["experiment_id"])
    archive = target / "archive" / period_name / revision
    archive.mkdir(parents=True, exist_ok=True)
    payloads = {"json": json_bytes, "csv": csv_export(report).encode(), "html": rendered_html}
    manifest = {"report_version": REPORT_VERSION, "experiment_id": report["experiment_id"], "revision": revision,
                "generated_at": report["generated_at"], "data_cutoff": report["data_cutoff"], "mode": report["mode"],
                "source_record_ids": report["source_record_ids"], "artifacts": {k: {"checksum": checksum(v), "size_bytes": len(v)} for k,v in payloads.items()}}
    payloads["manifest.json"] = canonical(manifest)
    for extension, payload in payloads.items():
        filename = "manifest.json" if extension == "manifest.json" else "report." + extension
        path = archive / filename
        if path.exists() and path.read_bytes() != payload:
            raise EvidenceError("Immutable report revision changed")
        if not path.exists():
            with path.open("xb") as file:
                file.write(payload)
        if artifact_store:
            key = f"modeling/v1/{safe_experiment}/{report['mode']}/{period_name}/{revision}/{filename}"
            artifact_store.put_once_or_verify("reports", key, payload, content_type={"html":"text/html", "csv":"text/csv"}.get(extension,"application/json"))
    for extension, payload in payloads.items():
        if extension == "manifest.json":
            continue
        for name in (f"{period_name}.{extension}", f"latest.{extension}"):
            path = target / name
            temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
            temporary.write_bytes(payload)
            os.replace(temporary, path)
    return {"directory": str(target), "archive": str(archive), "revision": revision, "manifest": manifest}
