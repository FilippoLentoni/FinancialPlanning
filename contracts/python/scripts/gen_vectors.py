#!/usr/bin/env python3
"""Reference generator for the cross-language test vectors.

Deliberately independent of the package's own helpers: it uses only the
``rfc8785`` PyPI package and :mod:`hashlib`, so the Python and TypeScript
implementations are both checked against an outside reference.

* ``fixtures/vectors/configuration_id.json``: ``configuration_id`` =
  ``"cfg_"`` + lowercase hex SHA-256 of the RFC 8785 (JCS) canonical JSON of the
  configuration document (spec platform-identifiers; design D2). Each case gives
  the input as raw JSON text (``input_json``, with arbitrary key order and
  whitespace), the canonical form and the expected identifier.
* ``fixtures/vectors/proxied_keys.json``: derived downstream idempotency key =
  ``"lt_"`` + lowercase hex SHA-256 of the UTF-8 string
  ``caller_identity|env|tool|idempotency_key`` (design D10, "Idempotency for
  proxied calls"), with no spaces around the separators.

Usage: ``uv run python scripts/gen_vectors.py [--root CONTRACTS_ROOT] [--check]``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import rfc8785

DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def configuration_id(doc: Any) -> tuple[str, str]:
    canonical = rfc8785.dumps(doc)
    return canonical.decode("utf-8"), "cfg_" + hashlib.sha256(canonical).hexdigest()


def proxied_key(caller: str, env: str, tool: str, key: str) -> str:
    return "lt_" + hashlib.sha256(f"{caller}|{env}|{tool}|{key}".encode("utf-8")).hexdigest()


BASE_PAYLOAD = (
    '{"strategy": "mean_variance", "universe": ["SPY", "CASH"], "risk_aversion": 2.0,\n'
    ' "lookback_days": 252, "constraints": {"long_only": true, "max_weight": 1.0}}'
)
CONFIG_INPUTS: list[tuple[str, str, str]] = [
    ("base", "Synthetic finance configuration.", '{"domain": "finance", "domain_schema_version": "1.0", "payload": ' + BASE_PAYLOAD + ', "synthetic": true}'),
    ("base-key-order-and-whitespace", "Same document as 'base' with different key order and whitespace: same configuration_id (ID-02).",
     '{\n  "synthetic":true,"payload":{"lookback_days":252,"constraints":{"max_weight":1.0,"long_only":true},\n "universe":["SPY","CASH"],"risk_aversion":2.0,"strategy":"mean_variance"},\n "domain_schema_version":"1.0","domain":"finance"}'),
    ("base-number-forms", "Same as 'base' with 2.0 written as 2 and 1.0 as 1e0: JCS number canonicalization gives the same configuration_id.",
     '{"domain":"finance","domain_schema_version":"1.0","payload":{"strategy":"mean_variance","universe":["SPY","CASH"],"risk_aversion":2,"lookback_days":252,"constraints":{"long_only":true,"max_weight":1e0}},"synthetic":true}'),
    ("risk-aversion-2-5", "Risk aversion changed from 2.0 to 2.5: different configuration_id (ID-02).",
     '{"domain": "finance", "domain_schema_version": "1.0", "payload": ' + BASE_PAYLOAD.replace('"risk_aversion": 2.0', '"risk_aversion": 2.5') + ', "synthetic": true}'),
    ("array-order-matters", "Array order is significant in JCS: reversed universe gives a different configuration_id.",
     '{"domain": "finance", "domain_schema_version": "1.0", "payload": ' + BASE_PAYLOAD.replace('["SPY", "CASH"]', '["CASH", "SPY"]') + ', "synthetic": true}'),
    ("unicode-and-escapes", "Non-ASCII and escaped characters are canonicalized per RFC 8785.",
     '{"payload":{"label":"Caf\\u00e9 \\"plan\\" \\u20ac","note":"line1\\nline2","tab":"a\\tb"},"domain_schema_version":"1.0","domain":"finance","synthetic":true}'),
    ("numbers", "Integer, negative, fractional, exponent and large values.",
     '{"domain":"finance","domain_schema_version":"1.0","payload":{"a":0,"b":-1,"c":0.1,"d":1e21,"e":1.5e-7,"f":1234567890,"g":-0.0},"synthetic":true}'),
    ("empty-payload", "Empty payload object.", '{"domain":"finance","domain_schema_version":"1.0","payload":{},"synthetic":true}'),
]

PROXIED_INPUTS: list[tuple[str, str, str, str, str, str]] = [
    ("caller-a", "Gateway caller A.", "gateway:beta:subject-a", "beta", "create_override_version", "client-key-0001"),
    ("caller-b-same-key", "Different caller, same key and tool: distinct derived key (ID-10).", "gateway:beta:subject-b", "beta", "create_override_version", "client-key-0001"),
    ("caller-a-retry", "Same caller retries with the same key: same derived key as 'caller-a' (ID-10).", "gateway:beta:subject-a", "beta", "create_override_version", "client-key-0001"),
    ("caller-a-other-env", "Same caller and key in gamma: distinct derived key.", "gateway:gamma:subject-a", "gamma", "create_override_version", "client-key-0001"),
    ("caller-a-other-tool", "Same caller and key, different tool: distinct derived key.", "gateway:beta:subject-a", "beta", "publish_plan_version", "client-key-0001"),
    ("direct-test", "Direct-test identity class.", "direct:beta", "beta", "refresh_market_data", "test-key_42"),
    ("max-length-key", "128-character client key.", "direct:prod", "prod", "submit_experiment", "k" * 128),
    ("non-ascii-caller", "Caller identity with non-ASCII characters is hashed as UTF-8.", "gateway:beta:subjéct", "beta", "validate_plan_version", "k1"),
]


def build() -> dict[str, dict[str, Any]]:
    cfg_cases = []
    for name, desc, raw in CONFIG_INPUTS:
        doc = json.loads(raw)
        canonical, cid = configuration_id(doc)
        cfg_cases.append({"name": name, "description": desc, "input_json": raw, "canonical_json": canonical, "configuration_id": cid})
    by = {c["name"]: c["configuration_id"] for c in cfg_cases}
    assert by["base"] == by["base-key-order-and-whitespace"] == by["base-number-forms"]
    assert len({by["base"], by["risk-aversion-2-5"], by["array-order-matters"]}) == 3

    pk_cases = []
    for name, desc, caller, env, tool, key in PROXIED_INPUTS:
        pk_cases.append({"name": name, "description": desc, "caller_identity": caller, "env": env, "tool": tool, "idempotency_key": key, "preimage": f"{caller}|{env}|{tool}|{key}", "derived_key": proxied_key(caller, env, tool, key)})
    pb = {c["name"]: c["derived_key"] for c in pk_cases}
    assert pb["caller-a"] == pb["caller-a-retry"] and pb["caller-a"] != pb["caller-b-same-key"]

    return {
        "configuration_id.json": {
            "synthetic": True,
            "description": "configuration_id = 'cfg_' + lowercase hex SHA-256 of the RFC 8785 canonical JSON of the configuration document. Parse input_json, canonicalize, hash.",
            "algorithm": {"canonicalization": "RFC 8785 (JCS)", "hash": "SHA-256", "encoding": "UTF-8", "prefix": "cfg_"},
            "equal_groups": [["base", "base-key-order-and-whitespace", "base-number-forms"]],
            "cases": cfg_cases,
        },
        "proxied_keys.json": {
            "synthetic": True,
            "description": "derived_key = 'lt_' + lowercase hex SHA-256 of the UTF-8 string 'caller_identity|env|tool|idempotency_key' (no spaces around '|').",
            "algorithm": {"preimage": "caller_identity|env|tool|idempotency_key", "hash": "SHA-256", "encoding": "UTF-8", "prefix": "lt_"},
            "equal_groups": [["caller-a", "caller-a-retry"]],
            "cases": pk_cases,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--check", action="store_true", help="fail if the committed vectors differ from a fresh computation")
    args = ap.parse_args(argv)
    out_dir = args.root / "fixtures" / "vectors"
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    for name, doc in build().items():
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        path = out_dir / name
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                print(f"{path}: out of date", file=sys.stderr)
                rc = 1
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
