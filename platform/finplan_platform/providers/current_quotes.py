"""Bounded unpaid current quotes, separate from model-native daily histories.

Yahoo's actual one-minute chart observations are quote evidence for valuation.
They are never converted into daily training bars or historical execution prices.
Raw responses, ordering, actual retrieval/availability and response checksums are
retained. Missing/stale feeds remain explicit, including market-closed periods.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
import math
import re
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


def _stamp(at):
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FinanceBetaPaperResearch/1"})
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("quote response exceeds byte limit")
    return raw


def decode_quote(symbol, raw, *, retrieved_at, cutoff=None, freshness_seconds=5400):
    retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    eligible = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) if cutoff else retrieved
    if retrieved > eligible:
        return {"instrument_id": symbol, "status": "unavailable_after_cutoff", "available_at": retrieved_at}
    data = json.loads(raw)
    rows = (data.get("chart") or {}).get("result") or []
    if len(rows) != 1:
        raise ValueError("quote provider returned no unique chart")
    row = rows[0]
    meta = row["meta"]
    if meta.get("symbol", "").replace("-", ".") != symbol.replace("-", "."):
        raise ValueError("quote symbol mismatch")
    if meta.get("currency") != "USD":
        raise ValueError("comparison quote currency must be USD")
    times = row.get("timestamp") or []
    quotes = row.get("indicators", {}).get("quote") or []
    if len(quotes) != 1:
        raise ValueError("quote columns missing")
    closes = quotes[0].get("close") or []
    if len(closes) != len(times) or times != sorted(set(times)):
        raise ValueError("quote timestamps/order/columns invalid")
    candidates = [(timestamp, price) for timestamp, price in zip(times, closes)
                  if isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) and price > 0
                  and datetime.fromtimestamp(timestamp + 60, UTC) <= eligible]
    if not candidates:
        raise ValueError("no eligible finite quote")
    timestamp, price = candidates[-1]
    index = times.index(timestamp)
    def numeric_column(name):
        values = quotes[0].get(name) or []
        value = values[index] if index < len(values) else None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
    quoted = datetime.fromtimestamp(timestamp + 60, UTC)
    period = meta.get("currentTradingPeriod", {}).get("regular") or {}
    regular_start, regular_end = period.get("start"), period.get("end")
    market_open = bool(regular_start and regular_end and regular_start <= eligible.timestamp() < regular_end)
    age = (eligible - quoted).total_seconds()
    completed_close = bool(not market_open and regular_start and regular_end
                           and quoted.timestamp() == regular_end and retrieved.timestamp() >= regular_end
                           and 0 <= age <= freshness_seconds)
    status = "fresh" if market_open and age <= freshness_seconds else "stale" if market_open else "fresh_close" if completed_close else "carried_forward_closed_market"
    return {"instrument_id": symbol, "price": float(price), "currency": "USD", "quoted_at": _stamp(quoted),
            "retrieved_at": retrieved_at, "available_at": retrieved_at, "status": status,
            "bar_started_at": _stamp(datetime.fromtimestamp(timestamp, UTC)), "bar_ended_at": _stamp(quoted),
            "open": numeric_column("open"), "high": numeric_column("high"), "low": numeric_column("low"),
            "volume": numeric_column("volume"), "volume_units": "shares_during_actual_minute_bar",
            "freshness_seconds": age, "freshness_limit_seconds": freshness_seconds, "market_open": market_open,
            "market_session": {"status": "completed" if completed_close else "open" if market_open else "closed",
                "session_date": datetime.fromtimestamp(regular_start, UTC).astimezone(ZoneInfo("America/New_York")).date().isoformat() if regular_start else None,
                "source": {"provider": "Yahoo Finance currentTradingPeriod regular; original response checksum",
                           "checksum": "sha256:" + hashlib.sha256(raw).hexdigest(),
                           "basis": "actual completed final minute at provider-declared regular session end" if completed_close else "provider-declared current session window"}},
            "provider": "Yahoo Finance public chart", "frequency": "1m", "price_basis": "unadjusted_last_completed_minute_close",
            "usage": "valuation_only_not_daily_model_input_or_execution_fill", "source_response_checksum": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "regular_session": {"start": _stamp(datetime.fromtimestamp(regular_start, UTC)) if regular_start else None,
                                "end": _stamp(datetime.fromtimestamp(regular_end, UTC)) if regular_end else None},
            "corporate_actions": row.get("events", {}),
            "corporate_action_coverage": {"provider": "Yahoo Finance public chart events",
                "request": "dividends_and_splits", "status": "provider_returned_events" if row.get("events") else "provider_returned_no_events",
                "window_start": _stamp(datetime.fromtimestamp(times[0], UTC)) if times else None,
                "window_end": _stamp(datetime.fromtimestamp(times[-1] + 60, UTC)) if times else None,
                "available_at": retrieved_at, "historical_vintage": "forward_collection_only"}}


def collect_quotes(symbols, *, cutoff=None, fetcher=None, max_requests=64):
    unique = sorted(set(symbols))
    if len(unique) > 600 or any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", symbol) for symbol in unique):
        raise ValueError("bounded validated quote universe required")
    if not 1 <= max_requests <= 64:
        raise ValueError("public quote request budget must be 1..64")
    fetcher = fetcher or _fetch
    def one(symbol):
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol.replace(".", "-"), safe="") + "?interval=1m&range=1d&events=div%2Csplits"
        try:
            raw = fetcher(url)
            retrieved = _stamp(datetime.now(UTC))
            quote = decode_quote(symbol, raw, retrieved_at=retrieved, cutoff=cutoff)
            quote["source_url"] = url
            artifact = {"instrument_id": symbol, "url": url, "retrieved_at": retrieved, "available_at": retrieved,
                        "original_content": raw.decode("utf-8"), "checksum": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "content_type": "application/json", "rights": "public_provider_quote_research_no_article_content"}
            return quote, artifact
        except Exception as exc:
            return {"instrument_id": symbol, "status": "failed", "error_code": type(exc).__name__, "http_status": getattr(exc, "code", None)}, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, unique[:max_requests]))
    quotes = [quote for quote, _ in results]
    quotes.extend({"instrument_id": symbol, "status": "skipped_request_budget"} for symbol in unique[max_requests:])
    return {"quotes": quotes, "source_artifacts": [artifact for _, artifact in results if artifact is not None],
            "available_at": _stamp(datetime.now(UTC)), "provider_cost_usd": 0,
            "cost_basis": "bounded public unpaid HTTP; no paid quote service invoked", "max_requests": max_requests,
            "coverage": {symbol: next(q["status"] for q in quotes if q["instrument_id"] == symbol) for symbol in unique}}
