"""End-of-day deep dive of an isolated beta modeling experiment, read from DynamoDB.

For one America/New_York day it lists, per portfolio: start/end value, every
buy/sell fill, and every hourly recommendation with the exact input and output
content each model saw and produced. Content comes from the DynamoDB copy that
``ModelingEvidence(mirror_content=True)`` writes; records written before that
copy existed fall back to the canonical S3 artifact and say so.

    python scripts/modeling_day_review.py --experiment beta-forward-hourly-v1-nine \\
        --day 2026-10-19 --output /home/ec2-user/projects/reports/beta-modeling/review
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "platform"))

from finplan_platform.core.errors import PlatformError  # noqa: E402
from finplan_platform.core.modeling_evidence import ModelingEvidence  # noqa: E402

TABLES = ("research_run", "research_input", "portfolio_observation")


def _rows(evidence: ModelingEvidence, table: str, experiment: str, day: str) -> list[dict[str, Any]]:
    rows, token = [], None
    for _ in range(1000):
        page, token = evidence.repo.query_index(table, "day-index", f"{experiment}#{day}", newest_first=False, limit=100, page_token=token)
        rows.extend(r.doc for r in page if r.doc)
        if not token:
            return rows
    raise RuntimeError("day exceeded 100,000 records")


def _content(evidence: ModelingEvidence, ref: dict[str, Any] | None, table: str) -> tuple[Any, str]:
    if not ref or not ref.get("artifact_key"):
        return None, "none"
    try:
        return evidence.read_content(ref, table=table), "dynamodb"
    except PlatformError:
        try:
            return json.loads(evidence.read_artifact_bytes(ref)), "s3_fallback"
        except PlatformError as exc:
            return {"evidence_error": exc.code}, "unavailable"


def _weights(output: Any) -> dict[str, float] | None:
    if not isinstance(output, dict):
        return None
    for holder in (output, (output.get("answer") or {}).get("recommendation") or {}):
        tw = holder.get("target_weights")
        if isinstance(tw, list):
            return {w["instrument_id"]: w["weight"] for w in tw if w.get("weight")}
        if isinstance(holder.get("weights"), dict):
            return {k: v for k, v in holder["weights"].items() if v}
    return None


def _rationale(output: Any) -> str | None:
    if not isinstance(output, dict):
        return None
    answer = output.get("answer") or {}
    text = output.get("rationale") or answer.get("narrative") or output.get("reason")
    return str(text) if text else None


def review(evidence: ModelingEvidence, experiment: str, day: str) -> dict[str, Any]:
    runs = _rows(evidence, "research_run", experiment, day)
    inputs = {d["input_id"]: d for d in _rows(evidence, "research_input", experiment, day)}
    observations = _rows(evidence, "portfolio_observation", experiment, day)
    books: dict[str, dict[str, Any]] = {}

    def book(approach: str, portfolio: str | None) -> dict[str, Any]:
        return books.setdefault(approach, {"approach_id": approach, "portfolio_id": portfolio, "valuations": [], "fills": [], "recommendations": []})

    sources: dict[str, int] = {}
    for run in sorted(runs, key=lambda r: r.get("slot", "")):
        if run.get("approach_id", "").startswith("__"):
            continue
        meta = run.get("input_id") and inputs.get(run["input_id"])
        model_input, in_src = _content(evidence, meta, "research_input") if meta else (None, "none")
        output, out_src = _content(evidence, run.get("output_ref"), "research_run")
        for s in (in_src, out_src):
            sources[s] = sources.get(s, 0) + 1
        book(run["approach_id"], run.get("portfolio_id"))["recommendations"].append({
            "slot": run.get("slot"), "run_id": run.get("run_id"), "status": run.get("status"),
            "target_weights": _weights(output), "rationale": _rationale(output),
            "input_id": run.get("input_id"), "input": model_input, "input_source": in_src,
            "output": output, "output_source": out_src})
    for obs in sorted(observations, key=lambda o: o.get("observed_at", "")):
        if obs.get("kind") not in ("valuation", "paper_execution"):
            continue
        payload, src = _content(evidence, obs, "portfolio_observation")
        sources[src] = sources.get(src, 0) + 1
        target = book(obs["approach_id"], obs.get("portfolio_id"))
        if obs["kind"] == "valuation" and isinstance(payload, dict):
            target["valuations"].append({"observed_at": obs.get("observed_at"), "slot": obs.get("slot"), "value": payload.get("value"),
                                         "cash": payload.get("cash"), "holdings": payload.get("holdings"), "quote_status": payload.get("quote_status")})
        elif obs["kind"] == "paper_execution" and isinstance(payload, dict):
            for fill in payload.get("simulated_fills") or []:
                target["fills"].append({"filled_at": obs.get("observed_at"), "decision_slot": obs.get("decision_slot"), "run_id": payload.get("run_id"), **fill})
    for b in books.values():
        values = [v["value"] for v in b["valuations"] if isinstance(v.get("value"), (int, float))]
        b["start_value"], b["end_value"] = (values[0], values[-1]) if values else (None, None)
        b["change_pct"] = round(100 * (b["end_value"] / b["start_value"] - 1), 4) if values and b["start_value"] else None
        b["buys"] = sum(1 for f in b["fills"] if f.get("side") == "buy")
        b["sells"] = sum(1 for f in b["fills"] if f.get("side") == "sell")
    ranking = sorted(books.values(), key=lambda b: (b["end_value"] is None, -(b["end_value"] or 0)))
    return {"experiment_id": experiment, "day": day, "content_sources": sources,
            "ranking": [{k: b[k] for k in ("approach_id", "start_value", "end_value", "change_pct", "buys", "sells")} for b in ranking],
            "portfolios": ranking}


def markdown(result: dict[str, Any]) -> str:
    out = [f"# {result['experiment_id']} — {result['day']} (America/New_York)", "",
           f"Content read from: {result['content_sources']}", "",
           "| Rank | Approach | Start | End | Change | Buys | Sells |", "|---|---|---|---|---|---|---|"]
    money = lambda v: "n/a" if v is None else f"${v:,.2f}"
    for i, r in enumerate(result["ranking"], 1):
        change = "n/a" if r["change_pct"] is None else f"{r['change_pct']:+.2f}%"
        out.append(f"| {i} | {r['approach_id']} | {money(r['start_value'])} | {money(r['end_value'])} | {change} | {r['buys']} | {r['sells']} |")
    for b in result["portfolios"]:
        out += ["", f"## {b['approach_id']}", "", "Buy/sell events:"]
        out += [f"- {f['filled_at']} {f['side'].upper()} {f['quantity']:.6g} {f['instrument_id']} @ {money(f['reference_price'])} (notional {money(f['notional'])}, cost {money(f['transaction_cost'])})" for f in b["fills"]] or ["- none"]
        out += ["", "Hourly recommendations:"]
        for r in b["recommendations"]:
            w = ", ".join(f"{k} {v:.1%}" for k, v in sorted((r["target_weights"] or {}).items(), key=lambda kv: -kv[1])) or "—"
            why = (r["rationale"] or "").replace("\n", " ")[:200]
            out.append(f"- {r['slot']} **{r['status']}** · {w}" + (f" · {why}" if why else ""))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--day", required=True, help="America/New_York date, YYYY-MM-DD")
    parser.add_argument("--output", type=Path, required=True, help="Directory for <experiment>-<day>.json/.md")
    args = parser.parse_args(argv)
    result = review(ModelingEvidence.from_ec2(), args.experiment, args.day)
    args.output.mkdir(parents=True, exist_ok=True)
    stem = args.output / f"{args.experiment}-{args.day}"
    stem.with_suffix(".json").write_text(json.dumps(result, indent=1, default=str))
    stem.with_suffix(".md").write_text(markdown(result))
    print(json.dumps({"json": str(stem.with_suffix(".json")), "markdown": str(stem.with_suffix(".md")), "ranking": result["ranking"], "content_sources": result["content_sources"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
