"""Platform errors expressed in the contract error vocabulary.

Every failure an operation reports is a :class:`PlatformError` carrying a registered
contract error code (``core/v1/error-codes.json`` of the pinned package). The default
``retryable`` value comes from that registry; codes whose ``retryable`` is fixed by the
contract cannot be overridden. :meth:`PlatformError.to_envelope` renders the
``core/v1/error.json`` envelope (with ``correlation_id`` and ``contract_version``) that
handlers return. Messages and details must never contain storage locations, ARNs,
secrets or stack traces; the envelope is validated by the contract ``no_leaks`` check in
the contract tests.

Usage::

    raise PlatformError.conflict("expected_revision does not match", expected_revision=4, current_revision=5)
    raise PlatformError("PRECONDITION_FAILED", "version is not validated", reason="not_validated")

Contract validation results map through :func:`from_validation`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from finplan_contracts.schemas import load_store
from finplan_contracts.validate import ValidationResult

__all__ = [
    "PlatformError",
    "registered_codes",
    "default_retryable",
    "from_validation",
    "VALIDATION_FAILED",
    "INVALID_IDENTIFIER",
    "NOT_FOUND",
    "CONFLICT",
    "IDEMPOTENCY_KEY_REUSED",
    "IMMUTABLE_RECORD",
    "PRECONDITION_FAILED",
    "UNAUTHORIZED",
    "FORBIDDEN",
    "OPERATION_NOT_PERMITTED",
    "BUDGET_EXCEEDED",
    "RATE_LIMITED",
    "DEPENDENCY_UNAVAILABLE",
    "UNSUPPORTED_CONTRACT_VERSION",
    "INTERNAL",
]

VALIDATION_FAILED = "VALIDATION_FAILED"
INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
NOT_FOUND = "NOT_FOUND"
CONFLICT = "CONFLICT"
IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
IMMUTABLE_RECORD = "IMMUTABLE_RECORD"
PRECONDITION_FAILED = "PRECONDITION_FAILED"
UNAUTHORIZED = "UNAUTHORIZED"
FORBIDDEN = "FORBIDDEN"
OPERATION_NOT_PERMITTED = "OPERATION_NOT_PERMITTED"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
RATE_LIMITED = "RATE_LIMITED"
DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
UNSUPPORTED_CONTRACT_VERSION = "UNSUPPORTED_CONTRACT_VERSION"
INTERNAL = "INTERNAL"


@lru_cache(maxsize=1)
def registered_codes() -> dict[str, dict[str, Any]]:
    """The pinned contract's registered error codes (code -> registration)."""
    return dict(load_store().get("error-codes").schema["x-finplan-error-codes"])


def default_retryable(code: str) -> bool:
    return bool(registered_codes()[code]["retryable"])


def _contract_version() -> str:
    return load_store().version


class PlatformError(Exception):
    """An operation failure with a registered contract error code."""

    def __init__(self, code: str, message: str, *, retryable: bool | None = None, details: dict[str, Any] | None = None, **detail_kwargs: Any) -> None:
        reg = registered_codes().get(code)
        if reg is None:
            raise ValueError(f"{code!r} is not a registered contract error code")
        if retryable is None:
            retryable = bool(reg["retryable"])
        elif reg.get("retryable_fixed") and retryable != bool(reg["retryable"]):
            raise ValueError(f"{code} has a fixed retryable value of {reg['retryable']}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = {**(details or {}), **detail_kwargs}

    # ------------------------------------------------------------ envelope
    def to_envelope(self, correlation_id: str, contract_version: str | None = None, *, synthetic: bool | None = None) -> dict[str, Any]:
        env: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
            "correlation_id": correlation_id,
            "contract_version": contract_version or _contract_version(),
        }
        if self.code == VALIDATION_FAILED:
            env["details"].setdefault("pointer", "")
        if synthetic is not None:
            env["synthetic"] = synthetic
        return env

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PlatformError({self.code!r}, {self.message!r}, retryable={self.retryable}, details={self.details!r})"

    # ------------------------------------------------------------ helpers
    @classmethod
    def validation(cls, message: str, pointer: str = "", **details: Any) -> "PlatformError":
        return cls(VALIDATION_FAILED, message, pointer=pointer, **details)

    @classmethod
    def not_found(cls, message: str, **details: Any) -> "PlatformError":
        return cls(NOT_FOUND, message, **details)

    @classmethod
    def conflict(cls, message: str, **details: Any) -> "PlatformError":
        return cls(CONFLICT, message, **details)

    @classmethod
    def precondition(cls, message: str, **details: Any) -> "PlatformError":
        return cls(PRECONDITION_FAILED, message, **details)

    @classmethod
    def immutable(cls, message: str, **details: Any) -> "PlatformError":
        return cls(IMMUTABLE_RECORD, message, **details)

    @classmethod
    def key_reused(cls, message: str = "idempotency_key was used with a different request", **details: Any) -> "PlatformError":
        return cls(IDEMPOTENCY_KEY_REUSED, message, **details)

    @classmethod
    def not_permitted(cls, message: str, **details: Any) -> "PlatformError":
        return cls(OPERATION_NOT_PERMITTED, message, **details)

    @classmethod
    def forbidden(cls, message: str, **details: Any) -> "PlatformError":
        return cls(FORBIDDEN, message, **details)

    @classmethod
    def internal(cls, message: str = "internal error", **details: Any) -> "PlatformError":
        return cls(INTERNAL, message, **details)


def from_validation(result: ValidationResult) -> PlatformError:
    """Map an invalid contract :class:`ValidationResult` to a :class:`PlatformError`.

    Reuses the package's own envelope builder so the code priority, ``pointer`` and
    ``field`` details are exactly the contract's.
    """
    env = result.to_error_envelope(correlation_id="cor_placeholder")
    return PlatformError(env["code"], env["message"], details=env["details"])
