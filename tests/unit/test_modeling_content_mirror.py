"""Every modeling artifact's exact bytes are also readable from DynamoDB (fake and moto)."""
import json
import os

import pytest

from finplan_platform.core import modeling_evidence as me
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.modeling_evidence import ModelingEvidence

AT = "2026-01-12T15:00:00Z"


@pytest.fixture
def evidence(repo_any, artifacts):
    return ModelingEvidence(repo_any, artifacts, mirror_content=True)


def _ready(e):
    book = e.ensure_portfolio("experiment-test", "optimizer", 1000, ["SPY"], AT)
    e.register_approaches("experiment-test", [{"approach_id": "optimizer", "family": "optimization"}], {"first_scheduled_slot": AT, "cadence_seconds": 3600}, AT)
    payload = {"availability_cutoff": AT, "decision_time": AT, "numeric": {"observation": [0.1, 0.2]}, "news": {"consumed": False, "items": []}, "holdings": book}
    source = e.capture_shared_source("experiment-test", AT, {"input_snapshot_id": "snap_01ARZ3NDEKTSV4RRFFQ69G5FAV", "prices": [{"symbol": "SPY", "price": 100, "available_at": AT}]}, AT)
    supplied = e.capture_input("experiment-test", "optimizer", AT, payload, source, AT)
    run = e.start_run("experiment-test", "optimizer", AT, supplied["input_id"], book["portfolio_id"], AT, {"family": "optimization"})
    return payload, supplied, run


def test_inputs_and_outputs_are_readable_from_dynamodb_alone(evidence):
    payload, supplied, run = _ready(evidence)
    assert evidence.read_content(supplied) == evidence.read_input(supplied["input_id"])["payload"]
    assert evidence.read_content(supplied)["numeric"] == payload["numeric"]
    outcome = {"status": "succeeded", "weights": {"SPY": 0.4}, "cash": 0.6, "rationale": "test", "forecast": {"available": False}}
    finished = evidence.finish_run(run["run_id"], outcome, AT)
    stored = evidence.read_content(finished["output_ref"])
    assert stored["weights"] == {"SPY": 0.4} and stored["rationale"] == "test"


def test_large_artifact_is_split_and_reassembled(evidence, monkeypatch):
    monkeypatch.setattr(me, "CONTENT_PART_BYTES", 64)
    blob = {"availability_cutoff": AT, "text": os.urandom(4000).hex()}
    ref = evidence._artifact("inputs", "mi_large", blob)
    name = evidence.repo.names["research_input"]
    first = evidence.repo.client.get_item(TableName=name, Key={"pk": {"S": f"content#{ref['artifact_key']}#0000"}})["Item"]
    assert int(first["parts"]["N"]) > 1
    assert evidence.read_content(ref) == json.loads(json.dumps(blob))


def test_identical_retry_is_idempotent_and_conflicting_bytes_are_rejected(evidence):
    ref = evidence._artifact("outputs", "mr_retry", {"a": 1})
    assert evidence._artifact("outputs", "mr_retry", {"a": 1}) == ref
    with pytest.raises(PlatformError):
        evidence._put_content("research_run", ref["artifact_key"], ref["checksum"], b"different bytes")


def test_mirror_is_off_unless_requested(repo_any, artifacts):
    plain = ModelingEvidence(repo_any, artifacts)
    ref = plain._artifact("outputs", "mr_plain", {"a": 1})
    with pytest.raises(PlatformError):
        plain.read_content(ref)


def test_day_review_reads_recommendations_and_valuations_from_dynamodb(evidence):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("modeling_day_review", Path(__file__).resolve().parents[2] / "scripts" / "modeling_day_review.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    _, supplied, run = _ready(evidence)
    evidence.finish_run(run["run_id"], {"status": "succeeded", "weights": {"SPY": 0.4}, "cash": 0.6, "rationale": "lowest variance", "forecast": {"available": False}}, AT)
    evidence.record_observation("experiment-test", "optimizer", AT, run["portfolio_id"], {"value": 1000, "cash": 1000, "holdings": []}, AT)
    result = module.review(evidence, "experiment-test", "2026-01-12")
    assert result["content_sources"] == {"dynamodb": 3}
    book = result["portfolios"][0]
    assert book["approach_id"] == "optimizer" and book["end_value"] == 1000
    rec = book["recommendations"][0]
    assert rec["target_weights"] == {"SPY": 0.4} and rec["rationale"] == "lowest variance"
    assert rec["input"]["numeric"] == {"observation": [0.1, 0.2]}
    assert "| 1 | optimizer |" in module.markdown(result)
