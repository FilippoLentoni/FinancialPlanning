"""ID-10: proxied-call idempotency key derivation (task 12.4, Python side)."""

from __future__ import annotations

import pytest

from finplan_contracts.keys import IDEMPOTENCY_KEY_PATTERN, derive_proxied_key, is_derived_key, proxied_preimage

from conftest import fixture

VECTORS = fixture("vectors/proxied_keys.json")


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_shared_vectors_reproduced(case):
    args = (case["caller_identity"], case["env"], case["tool"], case["idempotency_key"])
    assert proxied_preimage(*args) == case["preimage"]
    assert derive_proxied_key(*args) == case["derived_key"]


def test_two_callers_same_key_get_distinct_derived_keys():
    a = derive_proxied_key("gateway:beta:subject-a", "beta", "create_override_version", "client-key-0001")
    b = derive_proxied_key("gateway:beta:subject-b", "beta", "create_override_version", "client-key-0001")
    assert a != b


def test_same_caller_retry_gets_the_same_key():
    k1 = derive_proxied_key("gateway:gamma:subject-a", "gamma", "publish_plan_version", "retry-me")
    k2 = derive_proxied_key("gateway:gamma:subject-a", "gamma", "publish_plan_version", "retry-me")
    assert k1 == k2 and is_derived_key(k1)


def test_derived_key_matches_the_contract_patterns(store):
    import re

    k = derive_proxied_key("direct:beta", "beta", "refresh_market_data", "k")
    defs = store.get("idempotency").schema["$defs"]
    assert re.fullmatch(defs["derived_key"]["pattern"], k)
    assert re.fullmatch(defs["idempotency_key"]["pattern"], k)  # forwardable as a plain idempotency_key
    assert IDEMPOTENCY_KEY_PATTERN.match(k)


@pytest.mark.parametrize(
    "args",
    [
        ("", "beta", "get_plan", "k"),
        ("c", "dev", "get_plan", "k"),
        ("c", "beta", "Get-Plan", "k"),
        ("c", "beta", "get|plan", "k"),
        ("c", "beta", "get_plan", ""),
        ("c", "beta", "get_plan", "k" * 129),
        ("c", "beta", "get_plan", "bad|key"),
    ],
)
def test_invalid_inputs_rejected(args):
    with pytest.raises(ValueError):
        derive_proxied_key(*args)
