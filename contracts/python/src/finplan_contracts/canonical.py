"""RFC 8785 (JSON Canonicalization Scheme) and content-addressed identifiers.

* :func:`canonicalize` returns the RFC 8785 canonical UTF-8 bytes of a JSON value.
* :func:`configuration_id` = ``"cfg_"`` + lowercase hex SHA-256 of the canonical
  form of a configuration document (spec platform-identifiers, "Content-addressed
  configuration identifier"; design D2).
* :func:`request_hash` = ``"sha256:"`` + hex SHA-256 of the canonical request body,
  the idempotency request hash of ``core/v1/idempotency.json`` (transport headers
  such as the caller block are not part of the body).

This is an independent implementation of RFC 8785; the shared vectors in
``fixtures/vectors/configuration_id.json`` were produced with the ``rfc8785``
package, and the tests check both against each other.

Rules implemented (RFC 8785 section 3.2):

* objects: members sorted by the UTF-16 code units of their names, no whitespace;
* strings: only ``"``, ``\\`` and control characters below U+0020 are escaped
  (``\\b \\t \\n \\f \\r`` short forms, otherwise ``\\u00xx`` lowercase hex); every
  other character is emitted literally as UTF-8; lone surrogates are rejected;
* numbers: IEEE 754 doubles serialized like ECMAScript ``Number.prototype.toString``
  (shortest round-trip digits, ``-0`` becomes ``0``); NaN and infinities are
  rejected; integers outside the exactly representable double range are rejected
  rather than silently rounded;
* ``true``, ``false``, ``null`` literals.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

__all__ = [
    "CanonicalizationError",
    "CONFIGURATION_ID_PREFIX",
    "canonicalize",
    "canonical_text",
    "configuration_id",
    "configuration_id_from_json",
    "loads_strict",
    "request_hash",
    "sha256_hex",
]

CONFIGURATION_ID_PREFIX = "cfg_"
#: Largest safe integer (Number.MAX_SAFE_INTEGER, 2**53 - 1), as in the I-JSON profile used by RFC 8785.
_MAX_EXACT_INT = 2**53 - 1


class CanonicalizationError(ValueError):
    """The value cannot be canonicalized per RFC 8785 (I-JSON restrictions)."""


# ---------------------------------------------------------------- numbers
def _format_number(value: float) -> str:
    """ECMAScript Number.prototype.toString for a finite double."""
    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError("NaN and Infinity are not valid JSON numbers")
    if value == 0:
        return "0"  # also normalizes -0
    sign = "-" if value < 0 else ""
    # repr() gives the shortest digit string that round-trips (same as ECMAScript).
    r = repr(abs(value))
    if "e" in r or "E" in r:
        mantissa, exp_s = r.lower().split("e")
        exp = int(exp_s)
    else:
        mantissa, exp = r, 0
    if "." in mantissa:
        int_part, frac_part = mantissa.split(".")
    else:
        int_part, frac_part = mantissa, ""
    digits = (int_part + frac_part).lstrip("0")
    # position of the decimal point relative to the start of int_part
    point = len(int_part) + exp
    # leading zeros removed from digits shift the point
    leading = len(int_part + frac_part) - len((int_part + frac_part).lstrip("0"))
    n = point - leading  # value = 0.d1d2... * 10**n
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp_str = ("+" if e >= 0 else "-") + str(abs(e))
        out = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + exp_str
    return sign + out


def _number(value: int | float) -> str:
    if isinstance(value, int):
        if abs(value) > _MAX_EXACT_INT:
            raise CanonicalizationError(f"integer {value} is outside the IEEE 754 exact range (|n| <= 2**53 - 1)")
        return _format_number(float(value))
    return _format_number(value)


# ---------------------------------------------------------------- strings
_SHORT_ESCAPES = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"}


def _string(value: str) -> str:
    out = ['"']
    for ch in value:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise CanonicalizationError("strings must not contain lone surrogates")
        esc = _SHORT_ESCAPES.get(cp)
        if esc is not None:
            out.append(esc)
        elif cp < 0x20:
            out.append(f"\\u{cp:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(name: str) -> bytes:
    return name.encode("utf-16-be", errors="surrogatepass")


# ---------------------------------------------------------------- values
def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, (int, float)):
        out.append(_number(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise CanonicalizationError(f"object member names must be strings, got {type(k).__name__}")
        out.append("{")
        for i, k in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(_string(k))
            out.append(":")
            _serialize(value[k], out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    else:
        raise CanonicalizationError(f"value of type {type(value).__name__} is not JSON")


def canonical_text(value: Any) -> str:
    """RFC 8785 canonical JSON text of ``value``."""
    out: list[str] = []
    _serialize(value, out)
    return "".join(out)


def canonicalize(value: Any) -> bytes:
    """RFC 8785 canonical JSON of ``value`` as UTF-8 bytes."""
    return canonical_text(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise CanonicalizationError(f"duplicate object member name {k!r}")
        out[k] = v
    return out


def _reject_constant(name: str) -> Any:
    raise CanonicalizationError(f"{name} is not a valid JSON number")


def loads_strict(text: str | bytes) -> Any:
    """Parse JSON text, rejecting duplicate member names and NaN/Infinity (I-JSON)."""
    return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)


# ---------------------------------------------------------------- identifiers
def configuration_id(document: Any) -> str:
    """``cfg_`` + lowercase hex SHA-256 of the RFC 8785 canonical form of ``document``."""
    return CONFIGURATION_ID_PREFIX + sha256_hex(canonicalize(document))


def configuration_id_from_json(text: str | bytes) -> str:
    """``configuration_id`` of a configuration document given as raw JSON text."""
    return configuration_id(loads_strict(text))


def request_hash(body: Any) -> str:
    """Idempotency request hash: ``sha256:`` + hex SHA-256 of the canonical request body."""
    return "sha256:" + sha256_hex(canonicalize(body))
