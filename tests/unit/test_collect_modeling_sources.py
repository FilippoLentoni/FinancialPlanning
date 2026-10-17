"""Bounded collector CLI persistence/recovery through actual fake+moto storage adapters.

All provider payloads are explicitly controlled fixtures; this proves recovery and
readback, not live internet collection or forward price performance.
"""
import copy
import hashlib
import json

import pytest

from finplan_platform.core.modeling_evidence import ModelingEvidence, _id
from finplan_platform.providers import current_quotes
from scripts import collect_modeling_sources as cli

AT = "2026-01-12T15:00:00Z"


@pytest.fixture
def evidence_cli(repo_any, artifacts, monkeypatch):
    backend = ModelingEvidence(repo_any, artifacts)
    monkeypatch.setattr(ModelingEvidence, "from_ec2", classmethod(lambda cls, **kwargs: backend))
    return backend


def supplied():
    raw = '{"controlled_test":true,"price":100}'
    return {"available_at": AT, "status": "succeeded", "quotes": [{"instrument_id": "SPY", "price": 100,
                "quoted_at": AT, "available_at": AT, "status": "fresh", "provider": "controlled-test"}],
            "source_artifacts": [{"instrument_id": "SPY", "original_content": raw,
                "checksum": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(), "available_at": AT}],
            "coverage": {"SPY": "fresh"}, "provider_cost_usd": 0, "fixture_kind": "controlled_replay"}


def arguments(output, *extras):
    return ["--kind", "quotes", "--experiment", "collector-test", "--slot", AT,
            "--symbols", "SPY", "--output", str(output), *extras]


def stored(backend, experiment="collector-test"):
    iid = _id("mi", experiment, "__valuation_quotes__", AT, "valuation_quotes")
    return backend.read_input(iid), backend.read_input_bytes(iid)


def test_permanent_slot_retry_reuses_exact_original_bytes_without_provider_call(evidence_cli, monkeypatch, tmp_path):
    backend = evidence_cli
    calls = []
    def provider(symbols):
        calls.append(sorted(symbols))
        return supplied()
    monkeypatch.setattr(current_quotes, "collect_quotes", provider)
    assert cli.main(arguments(tmp_path)) == 0
    first, raw = stored(backend)
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda *_: pytest.fail("immutable retry invoked provider"))
    assert cli.main(arguments(tmp_path)) == 0
    second, retried = stored(backend)
    assert raw == retried and first == second
    assert calls == [["SPY"]]
    receipt = json.loads((tmp_path / "quotes-source-receipt.json").read_text())
    assert receipt["status"] == "reused_immutable_source"
    assert receipt["inputs"][0]["checksum"] == first["metadata"]["checksum"]
    assert first["payload"]["quotes"][0]["price"] == 100
    assert first["payload"]["collection_owner"] == "financialplanning"
    assert first["payload"]["evidence_purpose"] == "valuation_only"


def test_partial_cohort_commit_recovers_original_source_without_recapture(evidence_cli, monkeypatch, tmp_path):
    backend = evidence_cli
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda *_: supplied())
    capture = backend.capture_input
    def interrupted(experiment, *args, **kwargs):
        if experiment == "collector-test-eight":
            raise RuntimeError("controlled crash after first cohort commit")
        return capture(experiment, *args, **kwargs)
    monkeypatch.setattr(backend, "capture_input", interrupted)
    with pytest.raises(RuntimeError, match="controlled crash"):
        cli.main(arguments(tmp_path, "--include-experiment", "collector-test-eight"))
    first, raw = stored(backend)
    monkeypatch.setattr(backend, "capture_input", capture)
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda *_: pytest.fail("partial retry fetched replacement source"))
    assert cli.main(arguments(tmp_path, "--include-experiment", "collector-test-eight")) == 0
    second, other_raw = stored(backend, "collector-test-eight")
    assert raw == other_raw
    assert first["payload"] == second["payload"]
    assert first["metadata"]["checksum"] == second["metadata"]["checksum"]
    assert first["metadata"]["input_id"] != second["metadata"]["input_id"]
    assert stored(backend)[1] == raw


def test_global_provider_failure_is_persisted_and_queryable_not_lost(evidence_cli, monkeypatch, tmp_path):
    backend = evidence_cli
    def unavailable(*args, **kwargs):
        raise RuntimeError("controlled provider unavailable")
    monkeypatch.setattr(current_quotes, "collect_quotes", unavailable)
    result = cli.main(arguments(tmp_path))
    assert isinstance(result, int)
    captured, raw = stored(backend)
    payload = captured["payload"]
    assert payload.get("status") in {"failed", "unavailable"} or payload.get("provider_status") in {"failed", "unavailable"}
    assert "RuntimeError" in json.dumps(payload)
    assert "controlled provider unavailable" in json.dumps(payload)
    assert captured["metadata"]["checksum"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    rows, token = backend.query_day("collector-test", "2026-01-12", kind="inputs")
    assert token is None and captured["metadata"]["input_id"] in {r["input_id"] for r in rows}
    # A later recovery cannot hide the original failed provider attempt.
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda *_: pytest.fail("failed slot retried unbounded paid/network retrieval"))
    cli.main(arguments(tmp_path))
    assert stored(backend)[1] == raw


def test_shared_source_readback_retains_provider_original_content_and_all_cohort_refs(evidence_cli, monkeypatch, tmp_path):
    backend = evidence_cli
    expected = supplied()
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda *_: copy.deepcopy(expected))
    assert cli.main(arguments(tmp_path, "--include-experiment", "collector-test-eight")) == 0
    receipt = json.loads((tmp_path / "quotes-source-receipt.json").read_text())
    assert len(receipt["inputs"]) == 2
    for ref in receipt["inputs"]:
        actual = backend.read_input(ref["input_id"])
        assert actual["metadata"]["checksum"] == ref["checksum"]
        assert actual["payload"]["source_artifacts"] == expected["source_artifacts"]
        assert actual["payload"]["quotes"] == expected["quotes"]
        assert json.loads(backend.read_input_bytes(ref["input_id"])) == actual["payload"]


def test_duplicate_or_excess_cohorts_fail_before_provider(evidence_cli, monkeypatch, tmp_path):
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda *_: pytest.fail("invalid cohorts reached provider"))
    with pytest.raises(SystemExit):
        cli.main(arguments(tmp_path, "--include-experiment", "collector-test"))
    many = sum((["--include-experiment", "extra-"+str(i)] for i in range(8)), [])
    with pytest.raises(SystemExit):
        cli.main(arguments(tmp_path, *many))


def test_global_research_universe_failure_is_persisted_without_invented_news(evidence_cli, monkeypatch, tmp_path):
    from finplan_platform.providers import financial_research
    backend = evidence_cli
    def unavailable(*args, **kwargs):
        raise RuntimeError("controlled constituent universe unavailable")
    monkeypatch.setattr(financial_research, "collect", unavailable)
    result = cli.main(["--kind", "research", "--experiment", "collector-test", "--slot", AT, "--output", str(tmp_path)])
    assert isinstance(result, int)
    iid = _id("mi", "collector-test-interim", "__research_source__", AT, "research_source")
    actual = backend.read_input(iid)
    payload = actual["payload"]
    assert payload["status"] == "failed"
    assert payload["news"] == [] and payload["universe"] is None
    assert "controlled constituent universe unavailable" in json.dumps(payload)
    original = backend.read_input_bytes(iid)
    monkeypatch.setattr(financial_research, "collect", lambda *_: pytest.fail("failed research slot repeated provider calls"))
    cli.main(["--kind", "research", "--experiment", "collector-test", "--slot", AT, "--output", str(tmp_path)])
    assert backend.read_input_bytes(iid) == original
