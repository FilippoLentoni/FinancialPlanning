"""Core interfaces: clock, identifiers, context, errors (shared by every module)."""

from __future__ import annotations

import pytest
from finplan_contracts.validate import validate

from finplan_platform.core.clock import FrozenClock, parse_timestamp, to_timestamp
from finplan_platform.core.context import Caller, OperationContext
from finplan_platform.core.errors import PlatformError, from_validation, registered_codes
from finplan_platform.core.ids import PLATFORM_PREFIXES, IdFactory


def test_minted_ids_match_contract_identifier_formats() -> None:
    ids = IdFactory(FrozenClock())
    for field, prefix in PLATFORM_PREFIXES.items():
        value = ids.new(field)
        assert value.startswith(prefix + "_") and len(value) == len(prefix) + 27
    # validated through a real contract record
    doc = {"execution_id": ids.new("execution_id"), "publication_id": ids.new("publication_id"), "mode": "paper", "requested_at": "2026-01-12T14:30:00Z", "synthetic": True}
    assert validate(doc, "execution").valid


def test_ids_are_monotonic_under_a_frozen_clock() -> None:
    ids = IdFactory(FrozenClock())
    minted = [ids.new("pv") for _ in range(50)]
    assert minted == sorted(minted) and len(set(minted)) == 50


def test_platform_never_mints_foreign_ids() -> None:
    with pytest.raises(ValueError):
        IdFactory().new("run_id")
    with pytest.raises(ValueError):
        IdFactory().new("mv")


def test_frozen_clock_and_timestamps() -> None:
    c = FrozenClock("2026-03-08T06:59:59Z")
    c.advance(seconds=1)
    assert to_timestamp(c.now()) == "2026-03-08T07:00:00Z"
    assert parse_timestamp("2026-03-08T02:00:00-05:00") == parse_timestamp("2026-03-08T07:00:00Z")


def test_context_validates_env_and_correlation_id() -> None:
    with pytest.raises(ValueError):
        OperationContext(caller=Caller("p"), env="dev", correlation_id="cor_12345678")
    with pytest.raises(ValueError):
        OperationContext(caller=Caller("p"), env="beta", correlation_id="short")
    with pytest.raises(ValueError):
        Caller("")
    ctx = OperationContext.for_test()
    assert ctx.env == "beta" and ctx.caller.audit_view()["principal"]


def test_error_envelopes_validate_and_respect_fixed_retryable() -> None:
    for code in registered_codes():
        extra = {"served_contract_majors": [1]} if code == "UNSUPPORTED_CONTRACT_VERSION" else {}
        if code == "INVALID_IDENTIFIER":
            extra = {"field": "plan_version_id"}
        env = PlatformError(code, "synthetic message", **extra).to_envelope("cor_test00000001")
        assert validate(env, "error").valid, code
    with pytest.raises(ValueError):
        PlatformError("CONFLICT", "x", retryable=True)
    with pytest.raises(ValueError):
        PlatformError("NOT_A_CODE", "x")
    assert PlatformError("RATE_LIMITED", "x").retryable is True


def test_validation_result_maps_to_contract_error() -> None:
    bad = {"execution_id": "pv_01KDVDNAZ83BAMMYCEGWF33DPM", "publication_id": "pub_01KDVDNAZ83BAMMYCEGWF33DPM", "mode": "live", "requested_at": "2026-01-12T14:30:00Z"}
    err = from_validation(validate(bad, "execution"))
    assert err.code == "OPERATION_NOT_PERMITTED"
