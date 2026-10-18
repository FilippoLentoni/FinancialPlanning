"""Differential safety/no-lookahead checks for the merged input-only traversal."""
import math
import random
from datetime import datetime, timezone

import pytest
from finplan_platform.reporting.reports import (
    EvidenceError, SECRET_KEYS, _safe_content, _validate_input_content, timestamp,
)

CUTOFF = datetime(2026, 10, 18, 1, tzinfo=timezone.utc)
PAST = "2026-10-18T00:59:59Z"
FUTURE = "2026-10-18T01:00:01Z"


def legacy(value):
    def available(node):
        if isinstance(node, dict):
            if node.get("consumed") is False or node.get("eligible") is False:
                return
            for key in ("available_at", "platform_available_at", "availability_time", "availability_at"):
                if node.get(key) and timestamp(node[key]) > CUTOFF:
                    raise EvidenceError("Future-available evidence in captured model input")
            for child in node.values():
                available(child)
        elif isinstance(node, list):
            for child in node:
                available(child)
    available(value)
    _safe_content(value)


def result(function, value):
    try:
        function(value)
    except Exception as error:
        return type(error), str(error) if not isinstance(error, RecursionError) else "recursion"
    return None


def compare(value):
    assert result(legacy, value) == result(lambda v: _validate_input_content(v, CUTOFF), value)


@pytest.mark.parametrize("key", ["available_at", "platform_available_at", "availability_time", "availability_at"])
def test_exclusion_skips_only_availability_and_preserves_error_priority(key):
    compare({"items": [{key: FUTURE}]})
    compare({"consumed": False, "items": [{key: FUTURE}]})
    compare({"eligible": False, "items": [{key: "malformed", "children": [{key: FUTURE}]}]})
    for bad in [float("nan"), float("inf"), "-----BEGIN PRIVATE KEY", "https://example.test?X-Amz-Signature=secret"]:
        excluded = {"eligible": False, "nested": [{key: FUTURE, "bad": bad}]}
        compare(excluded)
        assert result(lambda v: _validate_input_content(v, CUTOFF), excluded) is not None
    # A later availability error still precedes an earlier safety error.
    compare({"bad": "-----BEGIN PRIVATE KEY", "later": {key: FUTURE}})
    compare({next(iter(SECRET_KEYS)).upper(): "private", "later": {key: FUTURE}})


@pytest.mark.parametrize("value", [None, False, True, 0, -100, 10**1000, -0.0, 1e-7, [], {}, "", [float("nan")], [float("-inf")], {"available_at": 5}, {"available_at": "bad"}, {"available_at": "2026-10-18T01:00:00"}, {"consumed": 0, "available_at": FUTURE}])
def test_scalar_numeric_and_malformed_values_match_legacy(value):
    compare(value)


def test_subclass_fallback_and_unknown_container_behavior_match_legacy():
    class Dict(dict): pass
    class List(list): pass
    class Float(float): pass
    class String(str): pass
    for value in [Dict(available_at=FUTURE), Dict(consumed=False, nested=List([Float("nan")])), List([String("-----BEGIN PRIVATE KEY")]), Float("inf"), String("X-Amz-Signature="), ("-----BEGIN PRIVATE KEY",), {"nested": (float("nan"),)}]:
        compare(value)


def test_deterministic_mixed_nested_inputs_match_legacy():
    rng = random.Random(713)
    leaves = [None, False, True, 0, 3, -0.0, 1.5, "normal", "-----BEGIN PRIVATE KEY", float("inf")]
    def tree(depth):
        if not depth or rng.random() < .4:
            return rng.choice(leaves)
        if rng.random() < .5:
            return [tree(depth-1) for _ in range(rng.randrange(5))]
        node = {f"field{i}": tree(depth-1) for i in range(rng.randrange(5))}
        if rng.random() < .5: node[rng.choice(["consumed", "eligible"])] = rng.choice([False, True, 0])
        if rng.random() < .5: node[rng.choice(["available_at", "platform_available_at", "availability_time", "availability_at"])] = rng.choice([PAST, FUTURE, None, "bad"])
        if rng.random() < .1: node[next(iter(SECRET_KEYS)).swapcase()] = "must refuse"
        return node
    for _ in range(600): compare(tree(6))
    deep = {"available_at": PAST}
    for _ in range(150): deep = {"children": [deep, 1.0, None]}
    compare(deep)


def test_input_validation_keeps_existing_raw_checksum_and_payload_identity_guards():
    from finplan_platform.reporting.reports import _input_for_run, checksum
    raw = b'{ "input_id":"mi_test", "run_id":"mr_test", "approach_id":"cash", "decision_time":"2026-10-18T01:00:00Z", "availability_cutoff":"2026-10-18T01:00:00Z", "numeric":[-0.0,1e-07,1.0000] }'
    import json
    payload = json.loads(raw)
    metadata = {"input_id":"mi_test", "approach_id":"cash", "checksum":checksum(raw), "kind":"model_input"}
    run = {"input_id":"mi_test", "run_id":"mr_test", "approach_id":"cash", "scheduled_slot":"2026-10-18T01:00:00Z", "decision_time":"2026-10-18T01:00:00Z"}
    class Reader:
        def read_input(self, key): return {"metadata":metadata, "payload":payload, "raw_bytes":raw}
    captured = _input_for_run(Reader(), dict(run))
    import base64
    assert base64.b64decode(captured["raw_base64"]) == raw
    with pytest.raises(EvidenceError, match="checksum"):
        _input_for_run(Reader(), dict(run), captured={"metadata":{**metadata,"checksum":"sha256:wrong"},"payload":payload,"raw_bytes":raw})
    with pytest.raises(EvidenceError, match="differs"):
        _input_for_run(Reader(), dict(run), captured={"metadata":metadata,"payload":{**payload,"numeric":[999]},"raw_bytes":raw})
