"""Cross-language vectors: configuration_id (ID-02) and proxied idempotency keys (ID-10)."""

import hashlib
import json
import subprocess
import sys

import rfc8785

from finplan_contracts.validate import validate

from conftest import CONTRACTS, FIXTURES


def load(name):
    return json.loads((FIXTURES / "vectors" / name).read_text(encoding="utf-8"))


def test_vectors_are_up_to_date():
    script = CONTRACTS / "python" / "scripts" / "gen_vectors.py"
    proc = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_configuration_id_vectors_recompute():
    data = load("configuration_id.json")
    assert data["synthetic"] is True
    for case in data["cases"]:
        canonical = rfc8785.dumps(json.loads(case["input_json"]))
        assert canonical.decode() == case["canonical_json"], case["name"]
        assert "cfg_" + hashlib.sha256(canonical).hexdigest() == case["configuration_id"], case["name"]
        assert validate({"configuration_id": case["configuration_id"]}, "identifiers").valid


def test_key_order_invariance_and_value_sensitivity():
    by = {c["name"]: c["configuration_id"] for c in load("configuration_id.json")["cases"]}
    assert by["base"] == by["base-key-order-and-whitespace"] == by["base-number-forms"]
    assert by["risk-aversion-2-5"] != by["base"] and by["array-order-matters"] != by["base"]


def test_configuration_vector_documents_are_contract_valid():
    for case in load("configuration_id.json")["cases"]:
        doc = json.loads(case["input_json"])
        if case["name"].startswith(("base", "risk", "array")):
            assert validate(doc, "configuration").valid, case["name"]


def test_proxied_keys():
    data = load("proxied_keys.json")
    by = {}
    for c in data["cases"]:
        pre = f"{c['caller_identity']}|{c['env']}|{c['tool']}|{c['idempotency_key']}"
        assert pre == c["preimage"]
        assert c["derived_key"] == "lt_" + hashlib.sha256(pre.encode("utf-8")).hexdigest()
        assert len(c["derived_key"]) == 67
        assert validate({"idempotency_key": c["derived_key"]} | {"scope": {"principal": "p", "environment": "beta", "operation": "op"}, "request_hash": "sha256:" + "0" * 64, "response": {}, "recorded_at": "2026-01-01T00:00:00Z", "retain_until": "2026-01-08T00:00:00Z"}, "idempotency").valid
        by[c["name"]] = c["derived_key"]
    assert by["caller-a"] == by["caller-a-retry"]
    assert len({by["caller-a"], by["caller-b-same-key"], by["caller-a-other-env"], by["caller-a-other-tool"]}) == 4
