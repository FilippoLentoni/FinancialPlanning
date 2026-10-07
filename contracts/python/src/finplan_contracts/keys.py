"""Idempotency keys for proxied calls (design D10; spec platform-identifiers, ID-10).

When a tool adapter proxies a state-changing call to a producer under its own
role, the producer's idempotency scope principal is the adapter. To keep two end
callers that happen to choose the same ``idempotency_key`` from colliding, the
adapter forwards the derived key::

    "lt_" + lowercase hex SHA-256( UTF-8 "caller_identity|env|tool|idempotency_key" )

with no spaces around the separators, together with a deterministic body (use
:func:`finplan_contracts.canonical.request_hash` for the body hash). The derived
key is 67 characters from ``[a-z0-9_]``, so it is itself a valid
``idempotency_key``.

The preimage is unambiguous: ``env`` is one of ``beta|gamma|prod``, ``tool`` is a
snake_case tool name and ``idempotency_key`` matches ``[A-Za-z0-9_-]{1,128}``, so
none of them contains ``|``; only ``caller_identity`` may, and it is the leftmost
field.
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["DERIVED_KEY_PREFIX", "DERIVED_KEY_PATTERN", "IDEMPOTENCY_KEY_PATTERN", "TOOL_NAME_PATTERN", "ENVIRONMENTS", "derive_proxied_key", "proxied_preimage", "is_derived_key"]

DERIVED_KEY_PREFIX = "lt_"
DERIVED_KEY_PATTERN = re.compile(r"^lt_[0-9a-f]{64}\Z")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}\Z")
ENVIRONMENTS = ("beta", "gamma", "prod")


def proxied_preimage(caller_identity: str, env: str, tool: str, idempotency_key: str) -> str:
    """The exact string hashed by :func:`derive_proxied_key` (after input validation)."""
    if not isinstance(caller_identity, str) or not caller_identity:
        raise ValueError("caller_identity must be a non-empty string")
    if env not in ENVIRONMENTS:
        raise ValueError(f"env must be one of {', '.join(ENVIRONMENTS)}, got {env!r}")
    if not isinstance(tool, str) or not TOOL_NAME_PATTERN.match(tool):
        raise ValueError(f"tool must be a snake_case tool name, got {tool!r}")
    if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_PATTERN.match(idempotency_key):
        raise ValueError("idempotency_key must be 1-128 characters from [A-Za-z0-9_-]")
    return f"{caller_identity}|{env}|{tool}|{idempotency_key}"


def derive_proxied_key(caller_identity: str, env: str, tool: str, idempotency_key: str) -> str:
    """Derived downstream idempotency key ``lt_<sha256 hex>`` (ID-10)."""
    preimage = proxied_preimage(caller_identity, env, tool, idempotency_key)
    return DERIVED_KEY_PREFIX + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def is_derived_key(value: str) -> bool:
    return isinstance(value, str) and bool(DERIVED_KEY_PATTERN.match(value))
