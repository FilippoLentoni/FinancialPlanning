"""ID-02: RFC 8785 canonicalization and content-addressed configuration_id (task 2.2, Python side)."""

from __future__ import annotations

import json
import math
import random

import pytest
import rfc8785

from finplan_contracts.canonical import (
    CanonicalizationError,
    canonical_text,
    canonicalize,
    configuration_id,
    configuration_id_from_json,
    loads_strict,
    request_hash,
)
from finplan_contracts.validate import validate

from conftest import fixture

VECTORS = fixture("vectors/configuration_id.json")


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_shared_vectors_reproduced(case):
    doc = loads_strict(case["input_json"])
    assert canonical_text(doc) == case["canonical_json"]
    assert configuration_id(doc) == case["configuration_id"]
    assert configuration_id_from_json(case["input_json"]) == case["configuration_id"]


def test_equal_groups_share_one_id_and_others_differ():
    by = {c["name"]: c["configuration_id"] for c in VECTORS["cases"]}
    for group in VECTORS["equal_groups"]:
        assert len({by[n] for n in group}) == 1
    assert len(set(by.values())) == len(by) - sum(len(g) - 1 for g in VECTORS["equal_groups"])


def test_key_order_and_whitespace_invariance():
    a = {"domain": "finance", "domain_schema_version": "1.0", "payload": {"risk_aversion": 2.0, "universe": ["SPY", "CASH"]}, "synthetic": True}
    b = json.loads('{ "synthetic" : true,\n "payload": {"universe":["SPY","CASH"], "risk_aversion": 2},"domain_schema_version":"1.0","domain":"finance"}')
    assert configuration_id(a) == configuration_id(b)


def test_value_sensitivity_risk_aversion():
    a = {"domain": "finance", "domain_schema_version": "1.0", "payload": {"risk_aversion": 2.0}, "synthetic": True}
    b = {"domain": "finance", "domain_schema_version": "1.0", "payload": {"risk_aversion": 2.5}, "synthetic": True}
    assert configuration_id(a) != configuration_id(b)


def test_configuration_id_matches_identifier_schema():
    cid = configuration_id({"domain": "finance", "payload": {}})
    assert cid.startswith("cfg_") and len(cid) == 68
    store_doc = fixture("job-status/valid/approved-gpu-queued.json") | {"configuration_id": cid}
    assert validate(store_doc, "job-status").valid


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.0, "0"), (-0.0, "0"), (1.0, "1"), (-1.5, "-1.5"), (0.1, "0.1"), (1e21, "1e+21"), (1e20, "100000000000000000000"),
        (1.5e-7, "1.5e-7"), (1e-6, "0.000001"), (1e-7, "1e-7"), (123456789.125, "123456789.125"), (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"), (2**53 - 1, "9007199254740991"), (333333333.33333329, "333333333.3333333"),
    ],
)
def test_number_serialization_matches_ecmascript(value, expected):
    assert canonical_text(value) == expected
    assert rfc8785.dumps(value).decode() == expected


def test_strings_and_key_sorting_by_utf16():
    doc = {"€": 1, "\r": 2, "1": 3, "\U0001f600": 4, "\u0080": 5, "דּ": 6, "a\u0000b": "\u001f\b\"\\/"}
    assert canonicalize(doc) == rfc8785.dumps(doc)


def test_rejects_non_ijson_values():
    for bad in (math.nan, math.inf, -math.inf, 2**53, {1: "x"}, {"a": object()}, "\ud800"):
        with pytest.raises(CanonicalizationError):
            canonicalize(bad)
    with pytest.raises(CanonicalizationError):
        loads_strict('{"a": 1, "a": 2}')
    with pytest.raises(CanonicalizationError):
        loads_strict('{"a": NaN}')


def _random_doc(rng: random.Random, depth: int = 0):
    kind = rng.randrange(7 if depth < 4 else 5)
    if kind == 0:
        return rng.choice([None, True, False])
    if kind == 1:
        return rng.randint(-(2**53 - 1), 2**53 - 1)
    if kind == 2:
        return rng.choice([rng.uniform(-1e6, 1e6), rng.random() * 10 ** rng.randint(-30, 30), float(rng.randint(-1000, 1000))])
    if kind in (3, 4):
        alphabet = "abcXYZ019 _-\"\\\n\té€\U0001f600\u0001"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
    if kind == 5:
        return [_random_doc(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {"".join(rng.choice("kKzé€\U0001f600_1") for _ in range(rng.randint(1, 4))): _random_doc(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def test_independent_implementation_agrees_with_rfc8785_reference():
    rng = random.Random(8785)
    for _ in range(500):
        doc = _random_doc(rng)
        assert canonicalize(doc) == rfc8785.dumps(doc)


def test_request_hash_ignores_key_order_and_uses_checksum_form():
    a = request_hash({"plan_id": "pl_01KDVDNAZ83BAMMYCEGWF33DPM", "allocations": [{"w": 0.5}]})
    b = request_hash(json.loads('{"allocations":[{"w":5e-1}],"plan_id":"pl_01KDVDNAZ83BAMMYCEGWF33DPM"}'))
    assert a == b and a.startswith("sha256:") and len(a) == 71
