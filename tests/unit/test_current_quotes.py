"""Known quote outcomes prove availability, stale handling and no invented bars."""
import json

import pytest

from finplan_platform.providers.current_quotes import decode_quote


def raw(**changes):
    row = {"meta": {"symbol": "AAPL", "currency": "USD", "currentTradingPeriod": {"regular": {"start": 1000, "end": 5000}}},
           "timestamp": [1100, 1200], "indicators": {"quote": [{"close": [100, 101], "volume": [10, 20]}]}}
    row.update(changes)
    return json.dumps({"chart": {"result": [row]}}).encode()


def test_actual_minute_values_and_order_preserved():
    quote = decode_quote("AAPL", raw(), retrieved_at="1970-01-01T00:25:00Z")
    assert quote["price"] == 101 and quote["quoted_at"] == "1970-01-01T00:21:00Z"
    assert quote["bar_started_at"] == "1970-01-01T00:20:00Z"
    assert quote["status"] == "fresh" and quote["usage"].startswith("valuation_only")
    assert quote["available_at"] == quote["retrieved_at"]
    assert quote["volume"] == 20 and quote["volume_units"] == "shares_during_actual_minute_bar"


def test_new_retrieval_cannot_enter_past_cutoff():
    quote = decode_quote("AAPL", raw(), retrieved_at="1970-01-01T00:25:00Z", cutoff="1970-01-01T00:20:00Z")
    assert quote["status"] == "unavailable_after_cutoff" and "price" not in quote


def test_future_provider_timestamp_cannot_be_selected():
    quote = decode_quote("AAPL", raw(), retrieved_at="1970-01-01T00:20:30Z")
    assert quote["price"] == 100


def test_closed_session_carries_actual_quote_without_fresh_claim():
    quote = decode_quote("AAPL", raw(), retrieved_at="1970-01-02T00:00:00Z")
    assert quote["price"] == 101 and quote["status"] == "carried_forward_closed_market"
    assert not quote["market_open"]


def test_actual_corporate_action_payload_is_retained_without_inventing_pay_date():
    events = {"splits": {"1100": {"date": 1100, "numerator": 2, "denominator": 1, "splitRatio": "2:1"}},
              "dividends": {"1200": {"date": 1200, "amount": 0.25}}}
    quote = decode_quote("AAPL", raw(events=events), retrieved_at="1970-01-01T00:25:00Z")
    assert quote["corporate_actions"] == events
    assert quote["corporate_action_coverage"]["historical_vintage"] == "forward_collection_only"
    assert "pay_date" not in quote["corporate_actions"]["dividends"]["1200"]


@pytest.mark.parametrize("changes", [{"timestamp": [1200, 1100]}, {"timestamp": [1100, 1100]}, {"meta": {"symbol": "WRONG", "currency": "USD"}}])
def test_invalid_provider_identity_and_order_are_rejected(changes):
    with pytest.raises(ValueError):
        decode_quote("AAPL", raw(**changes), retrieved_at="1970-01-01T00:25:00Z")


def test_actual_final_minute_remains_valid_close_without_offsession_fill_permission():
    data = raw(timestamp=[1100,4940])
    quote = decode_quote("AAPL", data, retrieved_at="1970-01-01T01:30:00Z")
    assert quote["status"] == "fresh_close" and quote["market_open"] is False
    assert quote["quoted_at"] == "1970-01-01T01:23:20Z"
    assert quote["market_session"]["status"] == "completed"
    # A partial final minute and an old close cannot be called the fresh close.
    partial = decode_quote("AAPL", raw(timestamp=[1100,4930]), retrieved_at="1970-01-01T01:30:00Z")
    assert partial["status"] == "carried_forward_closed_market"
    old = decode_quote("AAPL", data, retrieved_at="1970-01-02T01:30:00Z")
    assert old["status"] == "carried_forward_closed_market"
