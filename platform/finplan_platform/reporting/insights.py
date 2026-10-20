"""Explain the competition from persisted evidence: who is ahead, why, and why each model moved.

Everything here is derived arithmetic over the report's own rows. Nothing is re-run or
re-priced:

* Attribution: between consecutive valid valuations of one book, instrument ``i`` earns
  ``quantity_i(previous) * (price_i(now) - price_i(previous))`` at the persisted valuation
  quotes. The remainder of the value change (trading costs, fills at prices other than the
  valuation quote, corporate actions) is reported as ``costs_and_timing``; cash earns nothing.
* Head-to-head: the P&L gap between two books split into those same per-instrument terms.
* Recommendation changes: consecutive targets of one approach, with the captured inputs
  that differ between the two decisions (holdings drift, snapshot date, solver inputs,
  policy observation, news and rationale), chosen by the approach family.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable

INSIGHTS_VERSION = "modeling-insights/1"
TARGET_STATUSES = {"succeeded", "applied", "no_op", "hold", "reused", "recommended"}
DRIVER_LIMIT = 5
DRIFT_THRESHOLD = 0.005


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _weights(value: Any) -> dict[str, float] | None:
    if isinstance(value, list):
        return {row["instrument_id"]: float(row.get("weight") or 0.0) for row in value if isinstance(row, dict) and "instrument_id" in row}
    if isinstance(value, dict):
        return {k: float(v or 0.0) for k, v in value.items() if isinstance(v, (int, float))}
    return None


def _prices(obs: dict) -> dict[str, float]:
    prices = {}
    for instrument, quote in (obs.get("quote_provenance") or {}).items():
        price = quote.get("price") if isinstance(quote, dict) else None
        if isinstance(price, (int, float)) and math.isfinite(price):
            prices[instrument] = float(price)
    return prices


def _quantities(obs: dict) -> dict[str, float]:
    return {row["instrument_id"]: float(row.get("quantity") or 0.0) for row in obs.get("holdings") or [] if isinstance(row, dict) and "instrument_id" in row}


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _money(value: float) -> str:
    return ("-" if value < 0 else "+") + f"${abs(value):,.0f}"


def _book(aid: str, valuations: list[dict], capital: float | None, value_of: Callable[[dict], float | None]) -> dict:
    """Attribution and risk for one book over its valid valuation history."""
    points = [(o, value_of(o)) for o in valuations]
    points = [(o, v) for o, v in points if v is not None]
    contributions: dict[str, float] = {}
    residual = 0.0
    returns = []
    timeline = []
    previous = None
    start = capital if capital else (points[0][1] if points else None)
    if points and capital:
        # From inception (all cash) to the first valuation only costs/fills can move value.
        residual += points[0][1] - capital
        returns.append(points[0][1] / capital - 1)
    for obs, value in points:
        prices, quantities = _prices(obs), _quantities(obs)
        invested = sum(q * prices.get(i, 0.0) for i, q in quantities.items())
        timeline.append({"observed_at": obs["observed_at"], "observation_id": obs.get("observation_id"), "value": _round(value, 2),
                         "invested_fraction": _round(invested / value if value else None, 4),
                         "weights": {i: _round(q * prices[i] / value, 4) for i, q in quantities.items() if q and i in prices and value}})
        if previous is not None:
            prev_obs, prev_value, prev_prices, prev_quantities = previous
            explained = 0.0
            for instrument, quantity in prev_quantities.items():
                if quantity and instrument in prices and instrument in prev_prices:
                    gain = quantity * (prices[instrument] - prev_prices[instrument])
                    contributions[instrument] = contributions.get(instrument, 0.0) + gain
                    explained += gain
            residual += value - prev_value - explained
            if prev_value:
                returns.append(value / prev_value - 1)
        previous = (obs, value, prices, quantities)
    end = points[-1][1] if points else None
    peak, drawdown = start or 0.0, 0.0
    for _, value in points:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak if peak else 0.0)
    mean = sum(returns) / len(returns) if returns else None
    volatility = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) if len(returns) > 1 else None
    invested = [t["invested_fraction"] for t in timeline if t["invested_fraction"] is not None]
    pnl = end - start if end is not None and start else None
    ranked = sorted(contributions.items(), key=lambda kv: kv[1])
    return {"approach_id": aid, "valuations": len(points), "start_value": _round(start, 2), "end_value": _round(end, 2),
            "pnl": _round(pnl, 2), "return": _round(pnl / start if pnl is not None and start else None),
            "max_drawdown": _round(drawdown), "interval_volatility": _round(volatility),
            "average_invested_fraction": _round(sum(invested) / len(invested) if invested else None, 4),
            "contributions": {i: _round(v, 2) for i, v in sorted(contributions.items())},
            "costs_and_timing": _round(residual, 2),
            "best_instrument": ranked[-1][0] if ranked and ranked[-1][1] > 0 else None,
            "worst_instrument": ranked[0][0] if ranked and ranked[0][1] < 0 else None,
            "timeline": timeline}


def _compare(a: dict, b: dict, names: dict[str, str]) -> dict:
    """Why book ``a`` is ahead of (or behind) book ``b``, in dollars per instrument."""
    instruments = sorted(set(a["contributions"]) | set(b["contributions"]))
    rows = [{"instrument_id": i, "a": a["contributions"].get(i, 0.0), "b": b["contributions"].get(i, 0.0)} for i in instruments]
    for row in rows:
        row["difference"] = _round(row["a"] - row["b"], 2)
    rows.sort(key=lambda r: -abs(r["difference"]))
    gap = (a["pnl"] or 0.0) - (b["pnl"] or 0.0)
    costs = (a["costs_and_timing"] or 0.0) - (b["costs_and_timing"] or 0.0)
    na, nb = names.get(a["approach_id"], a["approach_id"]), names.get(b["approach_id"], b["approach_id"])
    lead = [r for r in rows if abs(r["difference"]) >= 0.5][:3]
    if abs(gap) < 0.5:
        text = f"{na} and {nb} are level: their holdings earned the same amounts."
    else:
        ahead, behind = (na, nb) if gap > 0 else (nb, na)
        parts = [f"{r['instrument_id']} ({na} {_money(r['a'])} vs {nb} {_money(r['b'])})" for r in lead]
        text = f"{ahead} is {_money(abs(gap))[1:]} ahead of {behind}."
        if parts:
            text += " Largest differences: " + "; ".join(parts) + "."
        if abs(costs) >= 0.5:
            text += f" Costs and fill timing account for {_money(costs)} of the gap ({na} minus {nb})."
        ia, ib = a.get("average_invested_fraction"), b.get("average_invested_fraction")
        if ia is not None and ib is not None and abs(ia - ib) >= 0.05:
            text += f" Average invested: {na} {ia:.0%}, {nb} {ib:.0%}."
    return {"a": a["approach_id"], "b": b["approach_id"], "pnl_gap": _round(gap, 2), "costs_and_timing_gap": _round(costs, 2),
            "instruments": rows, "summary": text}


def _vector_changes(names: list[str], before: list, after: list, label: str, scale: float = 1.0) -> list[dict]:
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after) or len(names) != len(after):
        return []
    rows = []
    for name, x, y in zip(names, before, after):
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            rows.append({"input": f"{label} {name}" if label else name, "before": _round(x * scale), "after": _round(y * scale),
                         "change": _round((y - x) * scale)})
    rows.sort(key=lambda r: -abs(r["change"] or 0.0))
    return [r for r in rows if r["change"]][:DRIVER_LIMIT]


def _input_drivers(previous: dict | None, current: dict | None, family: str, adapter: str) -> list[dict]:
    """Inputs that differ between two consecutive decisions of one approach."""
    if not previous or not current:
        return []
    drivers = []
    pp, cp = (previous.get("numeric") or {}).get("prepared") or {}, (current.get("numeric") or {}).get("prepared") or {}
    instruments = cp.get("instruments") or []
    if "expected_returns" in cp or "covariance" in cp:
        # Only the inputs the solver uses: min-variance ignores expected returns; CVaR uses the
        # scenario tail; mean-variance trades expected return against variance.
        algorithm = cp.get("algorithm")
        if algorithm in ("mean_variance", None):
            drivers += _vector_changes(instruments, pp.get("expected_returns"), cp.get("expected_returns"), "expected daily return")
        if algorithm == "cvar":
            drivers += _vector_changes(instruments, _tail(pp.get("scenarios"), len(instruments), cp.get("settings")),
                                       _tail(cp.get("scenarios"), len(instruments), cp.get("settings")), "scenario tail loss")
        vol = lambda p: [math.sqrt(p["covariance"][k][k]) if p["covariance"][k][k] >= 0 else None for k in range(len(instruments))]
        if pp.get("covariance") and cp.get("covariance") and len(pp["covariance"]) == len(cp["covariance"]) == len(instruments):
            drivers += _vector_changes(instruments, vol(pp), vol(cp), "daily volatility")
        drivers.sort(key=lambda r: -abs(r.get("change") or 0.0))
        drivers = drivers[:DRIVER_LIMIT]
    if cp.get("observation_names") and cp.get("observation"):
        drivers += _vector_changes(cp["observation_names"], pp.get("observation"), cp.get("observation"), "")
    before_news = {n.get("headline") for n in (previous.get("news") or {}).get("items") or [] if isinstance(n, dict)}
    fresh = [n.get("headline") for n in (current.get("news") or {}).get("items") or [] if isinstance(n, dict) and n.get("headline") not in before_news]
    if (current.get("news") or {}).get("consumed") and fresh:
        drivers.append({"input": "new headlines", "count": len(fresh), "examples": fresh[:DRIVER_LIMIT]})
    return drivers


def _tail(scenarios: Any, width: int, settings: Any) -> list | None:
    """Mean of each instrument's worst (1 - alpha) scenario returns, as a positive loss."""
    if not isinstance(scenarios, list) or not scenarios or any(not isinstance(row, list) or len(row) != width for row in scenarios):
        return None
    alpha = (settings or {}).get("cvar_alpha", 0.95) if isinstance(settings, dict) else 0.95
    count = max(1, int(round(len(scenarios) * (1 - alpha))))
    return [-sum(sorted(row[k] for row in scenarios)[:count]) / count for k in range(width)]


def input_digest(payload: Any) -> dict | None:
    """The parts of a captured input that the recommendation-change drivers read.

    Reports keep large captured inputs only as compressed original bytes plus a summary, so
    the drivers' fields are taken from the full payload while it is read and kept small.
    """
    if not isinstance(payload, dict):
        return None
    prepared = (payload.get("numeric") or {}).get("prepared") or {}
    kept = {k: prepared[k] for k in ("algorithm", "instruments", "expected_returns", "covariance", "scenarios", "settings",
                                     "observation_names", "observation") if k in prepared}
    if isinstance(prepared.get("environment"), dict):
        kept["environment"] = {k: prepared["environment"][k] for k in ("features", "rebalance_fraction") if k in prepared["environment"]}
    news = payload.get("news") if isinstance(payload.get("news"), dict) else {}
    holdings = payload.get("holdings") if isinstance(payload.get("holdings"), dict) else {}
    return {"holdings": {"weights": holdings.get("weights")}, "numeric": {"prepared": kept},
            "news": {"consumed": news.get("consumed"), "items": [{"headline": n.get("headline")} for n in news.get("items") or [] if isinstance(n, dict)]}}


def _evolution(aid: str, runs: list[dict], inputs: dict[str, dict], family: str, adapter: str, direct: str | None) -> list[dict]:
    rows, previous, previous_input = [], None, None
    for run in sorted(runs, key=lambda r: _ts(r["decision_time"])):
        output = run.get("output") or run.get("outcome") or {}
        target = _weights(run.get("target_weights", output.get("target_weights")))
        status = run.get("status")
        captured = (inputs.get(run["run_id"]) or {}).get("payload")
        entry = {"run_id": run["run_id"], "decision_time": run["decision_time"], "status": status,
                 "price_as_of": run.get("price_as_of", output.get("price_as_of"))}
        if target is None or status not in TARGET_STATUSES:
            entry["note"] = output.get("reason") or run.get("evidence_error") or "No target recommended at this decision."
            rows.append(entry)
            continue
        cash = run.get("cash_weight", output.get("cash_weight"))
        cash = float(cash) if isinstance(cash, (int, float)) else max(0.0, 1 - sum(target.values()))
        entry.update(target={i: _round(w, 4) for i, w in sorted(target.items())}, cash_weight=_round(cash, 4))
        held = _weights(((captured or {}).get("holdings") or {}).get("weights"))
        if previous is not None:
            before, before_cash = previous["target"], previous["cash_weight"]
            changes = {i: _round(target.get(i, 0.0) - before.get(i, 0.0), 4) for i in sorted(set(target) | set(before))}
            entry["change"] = {i: d for i, d in changes.items() if d}
            entry["cash_change"] = _round(cash - before_cash, 4)
            entry["turnover"] = _round((sum(abs(d) for d in changes.values()) + abs(cash - before_cash)) / 2, 4)
            reasons = []
            if entry["price_as_of"] and entry["price_as_of"] == previous.get("price_as_of"):
                reasons.append(f"Same daily price snapshot ({entry['price_as_of']}) as the previous decision; the market-data inputs did not change.")
            elif entry["price_as_of"]:
                reasons.append(f"New daily price snapshot: {previous.get('price_as_of')} → {entry['price_as_of']}.")
            if held:
                drift = max((abs(held.get(i, 0.0) - before.get(i, 0.0)), i) for i in set(held) | set(before))
                if drift[0] >= DRIFT_THRESHOLD:
                    reasons.append(f"Holdings drifted from the previous target before this decision (largest: {drift[1]} {before.get(drift[1], 0.0):.1%} → {held.get(drift[1], 0.0):.1%}).")
            environment = (((captured or {}).get("numeric") or {}).get("prepared") or {}).get("environment") or {}
            if adapter == "actor" and "current_weights" in (environment.get("features") or []):
                fraction = environment.get("rebalance_fraction")
                reasons.append("The policy observes its own current weights" + (" and moves only " + f"{fraction:.0%}" + " of the way to its target at each decision"
                               if isinstance(fraction, (int, float)) and fraction < 1 else "") + ", so the previous decision's fills change its next input.")
            if adapter == "agent_tool" and direct:
                reasons.append(f"Produced by the agent's tool call with the {direct} book's inputs; the drivers are that book's.")
            if entry["turnover"] < 0.0005:
                reasons.append("The recommendation is unchanged.")
            entry["reasons"] = reasons
        if previous is None:
            entry["reasons"] = ["First recommendation in this history."]
        entry["drivers"] = _input_drivers(previous_input, captured, family, adapter) if previous is not None else []
        rationale = run.get("rationale", output.get("rationale"))
        if isinstance(rationale, str) and rationale:
            entry["rationale"] = rationale
        rows.append(entry)
        previous, previous_input = entry, captured or previous_input
    return rows


def build_insights(*, registry: list[dict], runs: list[dict], observations: list[dict], inputs: dict[str, dict],
                   value_of: Callable[[dict], float | None], valid: Callable[[dict], bool], window: dict) -> dict:
    """Books, head-to-head explanations and recommendation changes for one experiment."""
    names = {a["approach_id"]: a.get("display_name") or a["approach_id"] for a in registry}
    known = {a["approach_id"]: a for a in registry}
    books = []
    for approach in registry:
        if approach.get("non_trading"):
            continue
        aid = approach["approach_id"]
        valuations = sorted([o for o in observations if o.get("approach_id") == aid and o.get("kind", "valuation") == "valuation" and valid(o)
                             and o.get("comparison_eligible") is not False], key=lambda o: (_ts(o["observed_at"]), o.get("observation_id", "")))
        capital = approach.get("starting_capital")
        book = _book(aid, valuations, float(capital) if isinstance(capital, (int, float)) else None, value_of)
        book.update(display_name=names[aid], family=approach.get("family"), adapter=approach.get("adapter"))
        books.append(book)
    ranked = sorted([b for b in books if b["pnl"] is not None], key=lambda b: (-b["pnl"], b["approach_id"]))
    for position, book in enumerate(ranked, 1):
        book["rank"] = position
    by_id = {b["approach_id"]: b for b in books}
    for book in books:
        book["summary"] = _book_summary(book)
    comparisons = [{**_compare(a, b, names), "kind": "next_ranked"} for a, b in zip(ranked, ranked[1:])]
    for aid, approach in known.items():
        direct = _direct_book(approach)
        if direct and aid in by_id and direct in by_id and by_id[aid]["pnl"] is not None and by_id[direct]["pnl"] is not None:
            pair = {aid, direct}
            comparisons = [c for c in comparisons if {c["a"], c["b"]} != pair]
            comparisons.append({**_compare(by_id[aid], by_id[direct], names), "kind": "agent_vs_direct"})
    evolution = {}
    for approach in registry:
        aid = approach["approach_id"]
        selected = [r for r in runs if r.get("approach_id") == aid]
        if selected:
            evolution[aid] = _evolution(aid, selected, inputs, approach.get("family") or "", approach.get("adapter") or "", _direct_book(approach))
    market = _market(observations, valid)
    invested = [b for b in ranked if (b.get("average_invested_fraction") or 0) >= 0.5]
    headline = []
    if ranked:
        headline.append(f"{ranked[0]['display_name']} leads with {_money(ranked[0]['pnl'])} over {ranked[0]['valuations']} valuations.")
    if invested and invested[0] is not (ranked[0] if ranked else None):
        headline.append(f"Best mostly-invested book: {invested[0]['display_name']} ({_money(invested[0]['pnl'])}).")
    if market:
        moves = sorted(market, key=lambda m: m["change"])
        headline.append(f"Over the same span the instruments moved from {moves[0]['instrument_id']} {moves[0]['change']:+.2%} to "
                        f"{moves[-1]['instrument_id']} {moves[-1]['change']:+.2%}.")
    first = min((o["observed_at"] for o in observations), default=None, key=_ts)
    last = max((o["observed_at"] for o in observations), default=None, key=_ts)
    return {"version": INSIGHTS_VERSION, "window": {**window, "first_observation": first, "last_observation": last},
            "method": ("Per-instrument P&L = previous quantity x change in persisted valuation price between consecutive valid "
                       "valuations; costs_and_timing is the rest of the value change (trading costs, fills away from the valuation "
                       "quote). Cash earns nothing. Rankings are by P&L since the first scheduled slot; short histories are not "
                       "evidence of skill."),
            "books": sorted(books, key=lambda b: (b.get("rank") or 10**6, b["approach_id"])),
            "headline": " ".join(headline), "market": market,
            "comparisons": comparisons, "evolution": evolution, "names": names}


def _book_summary(book: dict) -> str:
    if book["pnl"] is None:
        return "No valid valuations yet."
    parts = [f"{_money(book['pnl'])} ({book['return']:+.2%})" if book["return"] is not None else _money(book["pnl"])]
    contributions = book["contributions"]
    if book["best_instrument"]:
        parts.append(f"best {book['best_instrument']} {_money(contributions[book['best_instrument']])}")
    if book["worst_instrument"]:
        parts.append(f"worst {book['worst_instrument']} {_money(contributions[book['worst_instrument']])}")
    if book["costs_and_timing"]:
        parts.append(f"costs and fill timing {_money(book['costs_and_timing'])}")
    if book["average_invested_fraction"] is not None:
        parts.append(f"{book['average_invested_fraction']:.0%} invested on average")
    return "; ".join(parts) + "."


def _market(observations: list[dict], valid: Callable[[dict], bool]) -> list[dict]:
    """Each instrument's valuation-price change from its first to its last valid valuation quote."""
    first, last = {}, {}
    for obs in sorted([o for o in observations if o.get("kind", "valuation") == "valuation" and valid(o)], key=lambda o: _ts(o["observed_at"])):
        for instrument, price in _prices(obs).items():
            first.setdefault(instrument, (obs["observed_at"], price))
            last[instrument] = (obs["observed_at"], price)
    return [{"instrument_id": i, "first_price": _round(first[i][1], 4), "last_price": _round(last[i][1], 4),
             "first_at": first[i][0], "last_at": last[i][0], "change": _round(last[i][1] / first[i][1] - 1)}
            for i in sorted(first) if first[i][1]]


def _direct_book(approach: dict) -> str | None:
    """The direct book an agent-tool approach mirrors (agent_<model> -> its direct model book)."""
    if approach.get("adapter") != "agent_tool":
        return None
    model = approach.get("strategy_name") or approach["approach_id"].removeprefix("agent_")
    return {"min_variance": "classical_min_variance", "cvar": "classical_cvar", "ppo": "ppo"}.get(model)
