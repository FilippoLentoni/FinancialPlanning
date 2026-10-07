#!/usr/bin/env python3
"""Generate the deterministic synthetic fixtures and the conformance case manifest.

Writes ``contracts/fixtures/<schema-name>/{valid,invalid}/*.json``,
``contracts/conformance/cases.yaml`` and the generated part of
``contracts/fixtures/README.md``. Output is byte-for-byte deterministic: run it
twice and nothing changes. Every value is synthetic: identifiers are built from a
fixed timestamp and a counter, checksums are SHA-256 of labels, prices are round
made-up numbers and the lineage provider is the ``fixture`` provider.

Usage: ``uv run python scripts/gen_fixtures.py [--root CONTRACTS_ROOT]``
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import rfc8785
import yaml

HERE = Path(__file__).resolve()
DEFAULT_ROOT = HERE.parents[2]
BASE = "https://contracts.finplan.invalid/"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
TS0 = 1767225600000  # 2026-01-01T00:00:00Z, fixed so ULIDs are deterministic


# ------------------------------------------------------------------ builders
_DIGIT_RUN = re.compile(r"[0-9]{12}")


def _no_digit_run(make: Any) -> str:
    """First value from ``make(salt)`` without 12 consecutive digits, so fixture values never look
    like account IDs to an identifier scan. Deterministic."""
    salt = 0
    while True:
        value = make(salt)
        if not _DIGIT_RUN.search(value):
            return value
        salt += 1


def ulid(n: int) -> str:
    ts = TS0 + n * 1000
    t = "".join(CROCKFORD[(ts >> (5 * i)) & 31] for i in reversed(range(10)))

    def make(salt: int) -> str:
        rnd = int(hashlib.sha256(f"finplan-synthetic-ulid:{n}:{salt}".encode()).hexdigest()[:20], 16)
        return t + "".join(CROCKFORD[(rnd >> (5 * i)) & 31] for i in reversed(range(16)))

    return _no_digit_run(make)


def ident(prefix: str, n: int) -> str:
    return f"{prefix}_{ulid(n)}"


def chk(label: str) -> str:
    return _no_digit_run(lambda salt: "sha256:" + hashlib.sha256(f"finplan-synthetic:{label}:{salt}".encode()).hexdigest())


def ts(day: int, hour: int = 14, minute: int = 30) -> str:
    return f"2026-01-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def cfg_id(doc: dict[str, Any]) -> str:
    return "cfg_" + hashlib.sha256(rfc8785.dumps(doc)).hexdigest()


def sid(ns: str, name: str) -> str:
    return f"{BASE}{ns}/v1/{name}.json"


def lt_key(caller: str, env: str, tool: str, key: str) -> str:
    return "lt_" + hashlib.sha256(f"{caller}|{env}|{tool}|{key}".encode("utf-8")).hexdigest()


PF, PL = ident("pf", 1), ident("pl", 1)
PV_A, PV_B, PV_C, PV_D = (ident("pv", n) for n in (1, 2, 3, 4))
SNAP, SNAP2 = ident("snap", 1), ident("snap", 2)
MV = ident("mv", 1)
RUN, RUN2, RUN3 = ident("run", 1), ident("run", 2), ident("run", 3)
PUB, PUB2 = ident("pub", 1), ident("pub", 2)
EXE = ident("exe", 1)
REL0, REL1, REL2 = ident("rel", 0), ident("rel", 1), ident("rel", 2)
CORR = "corr-synthetic-0001"
CV = "1.0.0"
COMMIT = _no_digit_run(lambda salt: hashlib.sha1(f"finplan-synthetic-commit:{salt}".encode()).hexdigest())
ENV = "gamma"
INSTR = "SPY"
DATASET = f"finance/etf-daily/{INSTR}"
FINANCE = {"domain": "finance", "domain_schema_version": "1.0"}

CONFIG_PAYLOAD = {
    "strategy": "mean_variance",
    "objective": "portfolio_optimization",
    "universe": [INSTR, "CASH"],
    "risk_aversion": 2.0,
    "lookback_days": 252,
    "rebalance_frequency": "monthly",
    "constraints": {"long_only": True, "max_weight": 1.0},
    "fees": {"transaction_cost_bps": 5},
}
CONFIG = {**FINANCE, "payload": CONFIG_PAYLOAD, "synthetic": True}
CFG = cfg_id(CONFIG)

PLAN_CONTENT = {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": INSTR, "weight": 0.6}], "cash_weight": 0.4},
    "constraints": {"long_only": True, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5},
}


def aref(kind: str, n: int, owner: str = "financialplanning", content_type: str = "application/json") -> dict[str, Any]:
    return {"artifact_id": f"art_{ulid(n)}", "owner": owner, "kind": kind, "checksum": chk(f"{kind}-{n}"), "content_type": content_type}


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def err(code: str, retryable: bool, details: dict[str, Any] | None = None, message: str | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "message": message or f"synthetic {code.lower().replace('_', ' ')} error",
        "retryable": retryable,
        "details": details or {},
        "correlation_id": CORR,
        "contract_version": CV,
        "synthetic": True,
    }


SNAPSHOT = {
    "input_snapshot_id": SNAP,
    **FINANCE,
    "dataset": {"dataset_id": DATASET, "dataset_version": "fixture-1"},
    "manifest_checksum": chk("snapshot-manifest-1"),
    "source_timestamps": {"earliest": ts(2, 21, 0), "latest": ts(9, 21, 0)},
    "lineage": {"provider": "fixture", "provider_library": "finplan-fixture-provider", "library_version": "0.0.0", "retrieved_at": ts(10, 13, 0)},
    "coverage": {"start": "2026-01-02", "end": "2026-01-09"},
    "quality_flags": [],
    "status": "approved",
    "approval_rule_version": "auto-approve-v1",
    "artifacts": [aref("snapshot_manifest", 11), aref("snapshot_payload", 12)],
    "created_at": ts(10, 13, 5),
    "synthetic": True,
}

PV_ROOT = {
    "plan_version_id": PV_A,
    "plan_id": PL,
    "parent_plan_version_id": None,
    "input_snapshot_id": SNAP,
    "configuration_id": CFG,
    "model_version": MV,
    "run_id": RUN,
    "origin": "model_run",
    "status": "validated",
    "checksum": chk("plan-content-A"),
    **FINANCE,
    "content": PLAN_CONTENT,
    "content_ref": aref("plan_content", 21),
    "created_at": ts(11),
    "synthetic": True,
}

PUBLICATION = {
    "publication_id": PUB,
    "plan_id": PL,
    "plan_version_id": PV_A,
    "plan_version_checksum": chk("plan-content-A"),
    "plan_version_status": "validated",
    "supersedes_publication_id": None,
    "published_at": ts(12),
    "synthetic": True,
}

COST_CPU = {"estimated_usd_upper_bound": 0.35, "price_retrieved_at": ts(10, 9, 0), "remaining_allocation_usd": 6.2, "budget_category": "cpu_research", "synthetic": True}
COST_GPU = {"estimated_usd_upper_bound": 4.5, "price_retrieved_at": ts(10, 9, 0), "remaining_allocation_usd": 25, "budget_category": "gpu", "synthetic": True}

JOB_STATUS = {
    "run_id": RUN,
    "state": "running",
    "purpose": "research",
    "dry_run": False,
    "compute_class": "cpu",
    "cost_estimate": COST_CPU,
    "transitions": [{"state": "queued", "at": ts(10, 9, 1)}, {"state": "starting", "at": ts(10, 9, 2)}, {"state": "running", "at": ts(10, 9, 4)}],
    "elapsed_seconds": 120,
    "configuration_id": CFG,
    "input_snapshot_id": SNAP,
    "model_version": MV,
    "domain": "finance",
    "submitted_at": ts(10, 9, 0),
    "updated_at": ts(10, 9, 6),
    "synthetic": True,
}

RUN_PAYLOAD = {
    "performance": {"total_return": 0.012, "annualized_volatility": 0.1, "max_drawdown": -0.02},
    "accuracy": {"directional_hit_rate": 0.5},
    "compute_cost": {"estimated_usd": 0.35, "actual_usd": 0.12, "instance_seconds": 300},
    "proposed_allocation": {"weights": [{"instrument_id": INSTR, "weight": 0.6}], "cash_weight": 0.4},
}
JOB_RESULT = {
    "run_id": RUN,
    "completion_status": "succeeded",
    "solution_status": "optimal",
    "artifacts": [aref("run_artifact", 31, "financemodel")],
    "artifacts_complete": True,
    "model_version": MV,
    "configuration_id": CFG,
    "input_snapshot_id": SNAP,
    "evaluator_version": "eval-1.0.0",
    "dataset_checksum": chk("dataset-1"),
    **FINANCE,
    "payload": RUN_PAYLOAD,
    "completed_at": ts(10, 9, 30),
    "synthetic": True,
}

TOOLS = [
    # name, namespace of schemas, state_changing, role_class
    ("describe_capabilities", "core", False, "reader"),
    ("query_market_data", "finance", False, "reader"),
    ("get_plan", "core", False, "reader"),
    ("get_plan_version", "core", False, "reader"),
    ("list_plan_versions", "core", False, "reader"),
    ("get_job_status", "core", False, "reader"),
    ("get_experiment_result", "core", False, "reader"),
    ("refresh_market_data", "finance", True, "submitter"),
    ("submit_experiment", "core", True, "submitter"),
    ("create_override_version", "core", True, "plan-writer"),
    ("validate_plan_version", "core", True, "plan-writer"),
    ("publish_plan_version", "core", True, "plan-writer"),
]
MODEL_BACKED = {"submit_experiment", "get_job_status", "get_experiment_result"}


def tool_ids(name: str, ns: str) -> tuple[str, str]:
    kebab = name.replace("_", "-")
    return sid(ns, f"tools/{kebab}-request"), sid(ns, f"tools/{kebab}-response")


CATALOG = {
    "environment": ENV,
    "release_id": REL1,
    "contract_version": CV,
    "tools": [
        {
            "name": n,
            "description": f"Synthetic catalog entry for {n}.",
            "input_schema_id": tool_ids(n, ns)[0],
            "output_schema_id": tool_ids(n, ns)[1],
            "state_changing": sc,
            "role_class": rc,
            "lambda_ref_parameter": f"/finplan/{ENV}/financelambdastool/lambda/{n.replace('_', '-')}-arn",
        }
        for n, ns, sc, rc in TOOLS
    ],
    "synthetic": True,
}

CAPABILITY = {
    "environment": "beta",
    "release_id": REL1,
    "contract_version": CV,
    "served_contract_majors": [1],
    "tools": [
        {
            "name": n,
            "input_schema_id": tool_ids(n, ns)[0],
            "output_schema_id": tool_ids(n, ns)[1],
            "state_changing": sc,
            "available": n not in MODEL_BACKED,
            **({"unavailable_reason": "DEPENDENCY_UNAVAILABLE"} if n in MODEL_BACKED else {}),
        }
        for n, ns, sc, rc in TOOLS
    ],
    "generated_at": ts(10),
    "synthetic": True,
}

MANIFEST = {
    "repo": "financelambdastool",
    "environment": ENV,
    "region": "us-east-2",
    "release_id": REL1,
    "source_commit": COMMIT,
    "artifact_digest": chk("financelambdastool-artifact-rel1"),
    "contract_version": CV,
    "contract_digest": chk("contracts-1.0.0"),
    "deployed_at": ts(15),
    "previous_release_id": REL0,
    "outputs": {
        "tool-catalog": f"/finplan/{ENV}/financelambdastool/contract/tool-catalog",
        "get-plan-version-arn": f"/finplan/{ENV}/financelambdastool/lambda/get-plan-version-arn",
        "refresh-market-data-arn": f"/finplan/{ENV}/financelambdastool/lambda/refresh-market-data-arn",
        "role-reader-arn": f"/finplan/{ENV}/financelambdastool/lambda/role-reader-arn",
        "budget-enforced-role-names": f"/finplan/{ENV}/financelambdastool/config/budget-enforced-role-names",
    },
    "served_contract_majors": [1],
    "synthetic": True,
}

STAGED = {
    "run_id": RUN,
    "model_version": MV,
    "configuration_id": CFG,
    "input_snapshot_id": SNAP,
    "plan_id": PL,
    "parent_plan_version_id": PV_A,
    "completion_status": "succeeded",
    "solution_status": "optimal",
    "evaluator_version": "eval-1.0.0",
    "contract_version": CV,
    "files": [
        {"name": "plan-content.json", "checksum": chk("staged-plan-content"), "size_bytes": 512, "content_type": "application/json"},
        {"name": "metrics/summary.json", "checksum": chk("staged-metrics"), "size_bytes": 256, "content_type": "application/json"},
    ],
    **FINANCE,
    "payload": {"plan_content": PLAN_CONTENT, "metrics": {"expected_return": 0.05}},
    "written_at": ts(10, 9, 31),
    "synthetic": True,
}

IMPORT_ACCEPTED = {
    "plan_id": PL,
    "base_plan_version_id": PV_A,
    "created_plan_version_id": PV_C,
    "expected_revision": 2,
    "template_version": "xlsx-plan-v1",
    "source_artifact": aref("excel_source", 41, content_type=XLSX),
    "outcome": "accepted",
    "no_effect": False,
    "findings": [],
    "correlation_id": CORR,
    "created_at": ts(13),
    "synthetic": True,
}

def obs_fixture(session_date: str, close: float) -> dict[str, Any]:
    """One synthetic completed daily observation (round made-up numbers)."""
    return {"instrument_id": INSTR, "session_date": session_date, "kind": "completed_daily", "session_status": "regular", "open": close - 1.0, "high": close + 0.5, "low": close - 1.5, "close": close, "volume": 1000, "synthetic": True}


# ------------------------------------------------------------------ registry
CASES: list[dict[str, Any]] = []
FILES: dict[str, Any] = {}
SYNTHETIC_EXCEPTIONS: list[tuple[str, str]] = []


def add(schema: str, outcome: str, name: str, doc: Any, *, code: str | None = None, context: dict[str, Any] | None = None, covers: list[str] | None = None, exception: str | None = None) -> None:
    assert outcome in ("valid", "invalid")
    rel = f"{schema}/{outcome}/{name}.json"
    assert rel not in FILES, rel
    FILES[rel] = doc
    case: dict[str, Any] = {"fixture": rel, "schema": schema, "expect": outcome}
    if code:
        case["code"] = code
    if context:
        case["context"] = context
    if covers:
        case["covers"] = covers
    CASES.append(case)
    if exception:
        SYNTHETIC_EXCEPTIONS.append((rel, exception))


def V(schema: str, name: str, doc: Any, **kw: Any) -> None:
    add(schema, "valid", name, doc, **kw)


def I(schema: str, name: str, doc: Any, code: str = "VALIDATION_FAILED", **kw: Any) -> None:
    add(schema, "invalid", name, doc, code=code, **kw)


def mut(doc: dict[str, Any], **changes: Any) -> dict[str, Any]:
    d = copy.deepcopy(doc)
    for k, v in changes.items():
        if v is DROP:
            d.pop(k, None)
        else:
            d[k] = v
    return d


DROP = object()


def build() -> None:
    # ---------------------------------------------------------- identifiers
    all_ids = {
        "portfolio_id": PF, "plan_id": PL, "plan_version_id": PV_A, "input_snapshot_id": SNAP, "model_version": MV,
        "configuration_id": CFG, "run_id": RUN, "publication_id": PUB, "execution_id": EXE, "release_id": REL1, "synthetic": True,
    }
    V("identifiers", "all-identifiers", all_ids, covers=["ID-01"])
    V("identifiers", "spec-example-plan-version-id", {"plan_version_id": "pv_01JA2B3C4D5E6F7G8H9JKMNPQR", "synthetic": True}, covers=["ID-01"])
    wrong = {
        "portfolio_id": PL, "plan_id": PF, "plan_version_id": PL, "input_snapshot_id": PV_A, "model_version": RUN,
        "configuration_id": "cfg_" + "A" * 64, "run_id": MV, "publication_id": EXE, "execution_id": PUB,
    }
    for field, value in wrong.items():
        I("identifiers", f"wrong-prefix-{field.replace('_', '-')}", {field: value, "synthetic": True}, code="INVALID_IDENTIFIER", covers=["ID-01"])
    I("identifiers", "lowercase-ulid", {"plan_version_id": "pv_" + ulid(1).lower(), "synthetic": True}, code="INVALID_IDENTIFIER", covers=["ID-01"])
    I("identifiers", "ulid-forbidden-letter-u", {"run_id": "run_01JA2B3C4D5E6F7G8H9JKMNPQU", "synthetic": True}, code="INVALID_IDENTIFIER", covers=["ID-01"])
    I("identifiers", "ulid-too-short", {"plan_id": "pl_01JA2B3C4D", "synthetic": True}, code="INVALID_IDENTIFIER", covers=["ID-01"])
    I("identifiers", "unknown-identifier-field", {"plan_ver_id": PV_A, "synthetic": True}, covers=["ID-01"])

    # --------------------------------------------------------------- common
    common = {"checksum": chk("x"), "timestamp": ts(1), "date": "2026-01-01", "semver": "1.2.3", "environment": "beta", "repo": "financemodel", "domain": "finance", "domain_schema_version": "1.0", "correlation_id": CORR, "ssm_parameter_name": "/finplan/shared/financialplanning/config/budget-allocation", "schema_id": sid("core", "error"), "synthetic": True}
    V("common", "scalars", common)
    I("common", "checksum-uppercase-hex", {"checksum": "sha256:" + "A" * 64, "synthetic": True})
    I("common", "timestamp-without-zone", {"timestamp": "2026-01-01T10:00:00", "synthetic": True})
    I("common", "ssm-unknown-category", {"ssm_parameter_name": "/finplan/gamma/financemodel/secrets/jev-api-key", "synthetic": True}, covers=["ENV-07"])
    I("common", "ssm-unknown-environment", {"ssm_parameter_name": "/finplan/dev/financemodel/config/x", "synthetic": True}, covers=["ENV-07"])

    # ---------------------------------------------------------- idempotency
    record = {
        "scope": {"principal": "synthetic-role/plan-writer", "environment": "beta", "operation": "plan_version.create"},
        "idempotency_key": "client-key-0001",
        "request_hash": chk("request-body-1"),
        "response": {"plan_version_id": PV_B, "status": "pending_validation"},
        "replay_count": 0,
        "recorded_at": ts(5),
        "retain_until": ts(12),
        "synthetic": True,
    }
    V("idempotency", "completed-record", record, covers=["ID-10"])
    V("idempotency", "duplicate-request-replayed", mut(record, replay_count=1), covers=["ID-10"])
    V("idempotency", "proxied-derived-key", mut(record, idempotency_key=lt_key("gateway:beta", "beta", "create_override_version", "client-key-0001")), covers=["ID-10"])
    I("idempotency", "retention-under-7-days", mut(record, retain_until=ts(11)), covers=["ID-10"])
    I("idempotency", "key-too-long", mut(record, idempotency_key="k" * 129), covers=["ID-10"])
    I("idempotency", "key-bad-characters", mut(record, idempotency_key="key with spaces"), covers=["ID-10"])
    I("idempotency", "request-hash-not-sha256", mut(record, request_hash="md5:abc"), covers=["ID-10"])

    # ---------------------------------------------------------- concurrency
    V("concurrency", "head", {"revision": 5, "updated_at": ts(5), "synthetic": True}, covers=["ID-11"])
    V("concurrency", "initial-head", {"revision": 0, "synthetic": True}, covers=["ID-11"])
    I("concurrency", "negative-revision", {"revision": -1, "synthetic": True}, covers=["ID-11"])
    I("concurrency", "missing-revision", {"updated_at": ts(5), "synthetic": True}, covers=["ID-11"])
    I("concurrency", "string-revision", {"revision": "5", "synthetic": True}, covers=["ID-11"])

    # ---------------------------------------------------------- error codes
    codes = {
        "VALIDATION_FAILED": False, "INVALID_IDENTIFIER": False, "NOT_FOUND": False, "CONFLICT": False, "IDEMPOTENCY_KEY_REUSED": False,
        "IMMUTABLE_RECORD": False, "PRECONDITION_FAILED": False, "UNAUTHORIZED": False, "FORBIDDEN": False, "OPERATION_NOT_PERMITTED": False,
        "BUDGET_EXCEEDED": False, "RATE_LIMITED": True, "DEPENDENCY_UNAVAILABLE": True, "UNSUPPORTED_CONTRACT_VERSION": False, "INTERNAL": False,
    }
    for code, r in codes.items():
        V("error-codes", code.lower().replace("_", "-"), {"code": code, "retryable": r, "synthetic": True}, covers=["CS-06"])
    I("error-codes", "unregistered-code", {"code": "TEAPOT", "retryable": False, "synthetic": True}, covers=["CS-06"])
    I("error-codes", "validation-failed-retryable", {"code": "VALIDATION_FAILED", "retryable": True, "synthetic": True}, covers=["CS-06"])
    I("error-codes", "missing-retryable", {"code": "NOT_FOUND", "synthetic": True}, covers=["CS-06"])

    # ---------------------------------------------------------------- error
    details_for = {
        "VALIDATION_FAILED": {"pointer": "/allocation/weights/0/weight"},
        "INVALID_IDENTIFIER": {"field": "plan_version_id", "pointer": "/plan_version_id"},
        "NOT_FOUND": {"input_snapshot_id": SNAP},
        "CONFLICT": {"expected_revision": 4, "current_revision": 5, "hint": "re-read the plan head"},
        "IDEMPOTENCY_KEY_REUSED": {"idempotency_key": "client-key-0001"},
        "IMMUTABLE_RECORD": {"plan_version_id": PV_A, "hint": "create a child version"},
        "PRECONDITION_FAILED": {"plan_version_id": PV_B, "status": "pending_validation"},
        "UNAUTHORIZED": {},
        "FORBIDDEN": {"purpose": "production_candidate"},
        "OPERATION_NOT_PERMITTED": {"pointer": "/mode", "mode": "live"},
        "BUDGET_EXCEEDED": {"budget_category": "cpu_research", "estimated_usd_upper_bound": 9.0, "remaining_allocation_usd": 6.2},
        "RATE_LIMITED": {"dependency": "market-data-provider"},
        "DEPENDENCY_UNAVAILABLE": {"dependency": "financemodel", "environment": "beta"},
        "UNSUPPORTED_CONTRACT_VERSION": {"requested_major": 2, "served_contract_majors": [1]},
        "INTERNAL": {},
    }
    covers_for = {"BUDGET_EXCEEDED": ["CS-05", "ENV-17"], "OPERATION_NOT_PERMITTED": ["CS-05", "ID-09"], "IDEMPOTENCY_KEY_REUSED": ["CS-05", "ID-10"], "CONFLICT": ["CS-05", "ID-11"], "DEPENDENCY_UNAVAILABLE": ["CS-05", "OWN-06"]}
    for code, r in codes.items():
        V("error", code.lower().replace("_", "-"), err(code, r, details_for[code]), covers=covers_for.get(code, ["CS-05"]))
    V("error", "dependency-unavailable-not-retryable", err("DEPENDENCY_UNAVAILABLE", False, {"dependency": "financemodel"}), covers=["CS-05", "OWN-06"])
    I("error", "validation-failed-retryable", err("VALIDATION_FAILED", True, {"pointer": "/x"}), covers=["CS-06"])
    I("error", "validation-failed-without-pointer", err("VALIDATION_FAILED", False, {}), covers=["CS-05"])
    I("error", "invalid-identifier-without-field", err("INVALID_IDENTIFIER", False, {}), covers=["CS-05"])
    I("error", "unsupported-version-without-majors", err("UNSUPPORTED_CONTRACT_VERSION", False, {"requested_major": 2}), covers=["CS-05"])
    I("error", "stack-trace-field", mut(err("INTERNAL", False), stack_trace="synthetic"), covers=["CS-05"])
    I("error", "traceback-in-message", err("INTERNAL", False, message='Traceback (most recent call last): File "handler.py", line 10'), covers=["CS-05"])
    I("error", "storage-uri-in-details", err("NOT_FOUND", False, {"location": "s3://example-bucket/plans/x.json"}), covers=["CS-05", "CS-08"])
    I("error", "unregistered-code", err("TEAPOT", False), covers=["CS-06"])
    I("error", "missing-correlation-id", mut(err("INTERNAL", False), correlation_id=DROP), covers=["CS-05"])
    I("error", "missing-contract-version", mut(err("INTERNAL", False), contract_version=DROP), covers=["CS-05"])

    # ---------------------------------------------------------- artifact ref
    V("artifact-ref", "snapshot-manifest", {**aref("snapshot_manifest", 11), "size_bytes": 2048, "domain": "finance", "synthetic": True}, covers=["CS-08"])
    V("artifact-ref", "research-dataset", {**aref("research_dataset", 51, "financemodel"), "synthetic": True}, covers=["CS-08"])
    V("artifact-ref", "excel-source", {**aref("excel_source", 41, content_type=XLSX), "synthetic": True}, covers=["CS-08"])
    V("artifact-ref", "unknown-kind-from-later-minor", {**aref("plan_export", 52), "kind": "model_card", "synthetic": True}, covers=["CS-11"])
    I("artifact-ref", "s3-uri-artifact-id", {**aref("plan_content", 53), "artifact_id": "s3://example-bucket/plans/content.json", "synthetic": True}, covers=["CS-08"])
    I("artifact-ref", "path-artifact-id", {**aref("plan_content", 54), "artifact_id": "/data/plans/content.json", "synthetic": True}, covers=["CS-08"])
    I("artifact-ref", "bucket-and-key-fields", {**aref("plan_content", 55), "bucket": "example-bucket", "key": "plans/content.json", "synthetic": True}, covers=["CS-08"])
    I("artifact-ref", "missing-checksum", mut({**aref("plan_content", 56), "synthetic": True}, checksum=DROP), covers=["CS-08"])
    I("artifact-ref", "kind-not-vocabulary", {**aref("plan_content", 57), "kind": "Plan Content", "synthetic": True}, covers=["CS-11"])

    # --------------------------------------------------------------- caller
    caller = {"subject_hash": chk("subject-1"), "roles": ["planner"], "channel": "hosted_agent", "correlation_id": CORR, "synthetic": True}
    V("caller", "hosted-agent", caller)
    V("caller", "scheduler-no-subject", {"channel": "scheduler", "correlation_id": CORR, "synthetic": True})
    I("caller", "unknown-channel", mut(caller, channel="email"))
    I("caller", "missing-correlation-id", mut(caller, correlation_id=DROP))
    I("caller", "authority-field-not-allowed", mut(caller, grants=["plan-writer"]))

    # ------------------------------------------------------ domain registry
    reg = json.loads((ROOT / "domains" / "registry.json").read_text())
    V("domain-registry", "finance-only", {**reg, "synthetic": True}, covers=["DOM-02", "DOM-03"])
    bad = copy.deepcopy(reg)
    bad["domains"][0]["payload_schemas"]["configuration"] = sid("finance", "does-not-exist")
    I("domain-registry", "payload-schema-unresolved", {**bad, "synthetic": True}, covers=["DOM-02"])
    I("domain-registry", "no-domains", {"registry_version": 1, "domains": [], "synthetic": True}, covers=["DOM-02"])
    bad2 = copy.deepcopy(reg)
    bad2["domains"][0]["domain"] = "Supply-Chain"
    I("domain-registry", "bad-domain-key", {**bad2, "synthetic": True}, covers=["DOM-02"])

    # ------------------------------------------------------ domain envelope
    env_doc = {**FINANCE, "payload_kind": "plan_content", "payload": PLAN_CONTENT, "synthetic": True}
    V("domain-envelope", "finance-plan-content", env_doc, covers=["DOM-01", "DOM-03"])
    V("domain-envelope", "finance-configuration", {**FINANCE, "payload_kind": "configuration", "payload": CONFIG_PAYLOAD, "synthetic": True}, covers=["DOM-01"])
    I("domain-envelope", "unregistered-supply-chain", mut(env_doc, domain="supply_chain"), covers=["DOM-02"])
    I("domain-envelope", "unserved-domain-schema-version", mut(env_doc, domain_schema_version="9.0"), covers=["DOM-02"])
    I("domain-envelope", "finance-payload-invalid", mut(env_doc, payload={"base_currency": "USD"}), covers=["DOM-01"])
    I("domain-envelope", "unknown-payload-kind", mut(env_doc, payload_kind="shipment_plan"), covers=["DOM-02"])
    I("domain-envelope", "ticker-in-envelope", mut(env_doc, ticker=INSTR), covers=["DOM-01"])

    # -------------------------------------------------------- configuration
    V("configuration", "finance-mean-variance", CONFIG, covers=["ID-02", "DOM-01"])
    V("configuration", "finance-mean-variance-risk-2-5", {**CONFIG, "payload": {**CONFIG_PAYLOAD, "risk_aversion": 2.5}}, covers=["ID-02"])
    I("configuration", "unregistered-domain", mut(CONFIG, domain="supply_chain"), covers=["DOM-02"])
    I("configuration", "payload-unknown-field", mut(CONFIG, payload={**CONFIG_PAYLOAD, "ticker": INSTR}), covers=["DOM-01"])
    I("configuration", "missing-payload", mut(CONFIG, payload=DROP))

    # ------------------------------------------------------- input snapshot
    V("input-snapshot", "approved-etf-daily", SNAPSHOT, covers=["ID-05"])
    V("input-snapshot", "committed-missing-sessions-partial-response", mut(SNAPSHOT, status="committed", approval_rule_version=DROP, quality_flags=["missing_sessions", "partial_response"]), covers=["ID-05", "CS-11"])
    V("input-snapshot", "empty-response-no-new-observations", mut(SNAPSHOT, status="committed", approval_rule_version=DROP, quality_flags=["empty_response", "no_new_observations"]), covers=["ID-05", "CS-11"])
    V("input-snapshot", "unknown-quality-flag-from-later-minor", mut(SNAPSHOT, quality_flags=["stale_source", "provider_schema_changed"]), covers=["CS-11"])
    V("input-snapshot", "expired", mut(SNAPSHOT, status="expired"), covers=["ID-05", "CS-11"])
    platform_extras = mut(
        SNAPSHOT,
        status="committed",
        approval_rule_version=DROP,
        quality_flags=["missing_sessions", "rejected_records"],
        lineage={**SNAPSHOT["lineage"], "calendar_version": "fixture-synthetic-v1-20250101-20271231"},
        quality_details={"missing_sessions": ["2026-01-06"], "rejected_records": {"count": 1, "total_records": 6, "reasons": {"non_positive_close": 1}}},
        observation_summary={"completed_daily": 5, "intraday_partial": 0, "records_received": 6, "records_rejected": 1},
    )
    V("input-snapshot", "calendar-version-quality-details-observation-summary", platform_extras, covers=["ID-05", "CS-11"])
    I("input-snapshot", "quality-details-key-not-a-flag", mut(platform_extras, quality_details={"Missing Sessions": []}), covers=["CS-11"])
    I("input-snapshot", "observation-summary-negative-count", mut(platform_extras, observation_summary={"records_received": -1}), covers=["CS-11"])
    I("input-snapshot", "calendar-version-with-path", mut(platform_extras, lineage={**SNAPSHOT["lineage"], "calendar_version": "../calendars/xnys.json"}), covers=["CS-08"])
    V("input-snapshot", "schema-upgrade-without-provider-lineage-fields", mut(SNAPSHOT, lineage={"provider": "mock", "retrieved_at": ts(10, 13, 0)}), covers=["ID-05", "CS-03"])
    I("input-snapshot", "approved-without-rule-version", mut(SNAPSHOT, approval_rule_version=DROP), covers=["CS-11"])
    I("input-snapshot", "unknown-status", mut(SNAPSHOT, status="published"), covers=["CS-11"])
    I("input-snapshot", "missing-manifest-checksum", mut(SNAPSHOT, manifest_checksum=DROP), covers=["ID-05"])
    I("input-snapshot", "missing-coverage", mut(SNAPSHOT, coverage=DROP), covers=["ID-05"])
    I("input-snapshot", "wrong-prefix-snapshot-id", mut(SNAPSHOT, input_snapshot_id=PV_A), code="INVALID_IDENTIFIER", covers=["ID-01", "ID-05"])
    I("input-snapshot", "unregistered-domain", mut(SNAPSHOT, domain="supply_chain"), covers=["DOM-02"])
    I("input-snapshot", "lineage-missing-retrieved-at", mut(SNAPSHOT, lineage={"provider": "fixture"}), covers=["ID-05"])
    I("input-snapshot", "quality-flag-not-vocabulary", mut(SNAPSHOT, quality_flags=["Partial Response"]), covers=["CS-11"])

    # ----------------------------------------------------------------- plan
    plan = {"plan_id": PL, "portfolio_id": PF, "name": "Synthetic plan", "head": {"current_version_id": PV_B, "revision": 2}, "current_publication_id": PUB, "created_at": ts(3), "synthetic": True}
    V("plan", "plan-with-head", plan, covers=["ID-11"])
    V("plan", "new-plan-without-version", mut(plan, head={"current_version_id": None, "revision": 0}, current_publication_id=None))
    V("plan", "with-publication-revision", mut(plan, publication_revision=3), covers=["ID-08", "ID-11"])
    I("plan", "negative-publication-revision", mut(plan, publication_revision=-1), covers=["ID-11"])
    I("plan", "negative-revision", mut(plan, head={"current_version_id": PV_B, "revision": -1}), covers=["ID-11"])
    I("plan", "head-points-to-plan-id", mut(plan, head={"current_version_id": PL, "revision": 2}), code="INVALID_IDENTIFIER", covers=["ID-01"])
    I("plan", "missing-head", mut(plan, head=DROP))

    # --------------------------------------------------------- plan version
    child = mut(PV_ROOT, plan_version_id=PV_B, parent_plan_version_id=PV_A, run_id=None, origin="manual_override", status="pending_validation", checksum=chk("plan-content-B"), content_ref=aref("plan_content", 22), created_at=ts(12))
    excel = mut(child, plan_version_id=PV_C, parent_plan_version_id=PV_B, origin="excel_import", checksum=chk("plan-content-C"), source_artifact=aref("excel_source", 41, content_type=XLSX), created_at=ts(13))
    V("plan-version", "root-model-run", PV_ROOT, covers=["ID-06"])
    V("plan-version", "manual-override-child", child, covers=["ID-06"])
    V("plan-version", "excel-import-child", excel, covers=["ID-06"])
    V("plan-version", "no-effect-override", mut(child, plan_version_id=PV_D, parent_plan_version_id=PV_A, checksum=PV_ROOT["checksum"], no_effect=True), covers=["ID-06"])
    V("plan-version", "invalid-partial-output", mut(PV_ROOT, plan_version_id=PV_D, run_id=RUN2, status="invalid", validation_errors=[err("VALIDATION_FAILED", False, {"pointer": "/allocation/weights", "missing_instruments": ["CASH"]}, "required instruments missing from worker output")]), covers=["ID-07"])
    V("plan-version", "content-by-reference", mut(PV_ROOT, content=DROP), covers=["ID-06"])
    I("plan-version", "override-without-parent", mut(child, parent_plan_version_id=None), code="INVALID_IDENTIFIER", covers=["ID-06"])
    I("plan-version", "manual-override-with-run-id", mut(child, run_id=RUN), covers=["ID-06"])
    I("plan-version", "model-run-without-run-id", mut(PV_ROOT, run_id=None), code="INVALID_IDENTIFIER", covers=["ID-06"])
    I("plan-version", "excel-import-without-source-artifact", mut(excel, source_artifact=DROP), covers=["ID-06"])
    I("plan-version", "excel-import-wrong-source-kind", mut(excel, source_artifact=aref("plan_export", 42)), covers=["ID-06"])
    I("plan-version", "invalid-status-without-errors", mut(PV_ROOT, status="invalid"), covers=["ID-07"])
    I("plan-version", "unknown-origin", mut(PV_ROOT, origin="api_edit"), covers=["ID-06"])
    I("plan-version", "unknown-status", mut(PV_ROOT, status="published"), covers=["ID-07"])
    I("plan-version", "parent-wrong-prefix", mut(child, parent_plan_version_id=PL), code="INVALID_IDENTIFIER", covers=["ID-01", "ID-06"])
    I("plan-version", "content-not-finance-payload", mut(PV_ROOT, content={"base_currency": "USD"}), covers=["DOM-01"])
    I("plan-version", "missing-checksum", mut(PV_ROOT, checksum=DROP), covers=["ID-06"])

    # ---------------------------------------------------------- publication
    V("publication", "publication", PUBLICATION, covers=["ID-08"])
    V("publication", "superseding-publication", mut(PUBLICATION, publication_id=PUB2, plan_version_id=PV_C, plan_version_checksum=chk("plan-content-C"), supersedes_publication_id=PUB, published_at=ts(14)), covers=["ID-08"])
    I("publication", "unvalidated-version-status", mut(PUBLICATION, plan_version_status="pending_validation"), covers=["ID-08"])
    I("publication", "missing-checksum", mut(PUBLICATION, plan_version_checksum=DROP), covers=["ID-08"])
    I("publication", "plan-version-id-wrong-prefix", mut(PUBLICATION, plan_version_id=PUB), code="INVALID_IDENTIFIER", covers=["ID-01", "ID-08"])

    # ------------------------------------------------------------ execution
    exe = {"execution_id": EXE, "publication_id": PUB, "mode": "paper", "status": "recorded", "requested_at": ts(14), "synthetic": True}
    V("execution", "paper", exe, covers=["ID-09"])
    V("execution", "simulated", mut(exe, mode="simulated"), covers=["ID-09"])
    I("execution", "live-mode", mut(exe, mode="live"), code="OPERATION_NOT_PERMITTED", covers=["ID-09"])
    I("execution", "missing-publication", mut(exe, publication_id=DROP), covers=["ID-09"])
    I("execution", "publication-id-wrong-prefix", mut(exe, publication_id=PV_A), code="INVALID_IDENTIFIER", covers=["ID-01", "ID-09"])

    # ----------------------------------------------------- budget allocation
    reason = "budget-allocation is a closed category -> USD map (the SSM value), so it has no 'synthetic' key"
    defaults = {"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 25, "reserve": 5}
    V("budget-allocation", "defaults", defaults, covers=["ENV-17"], exception=reason)
    V("budget-allocation", "user-adjusted-under-ceiling", {**defaults, "gpu": 20, "platform_infra": 6}, covers=["ENV-17"], exception=reason)
    V("budget-allocation", "raised-ceiling-context", {**defaults, "gpu": 30}, context={"cost_ceiling_usd": 60}, covers=["ENV-17"], exception=reason)
    I("budget-allocation", "sum-above-ceiling", {**defaults, "gpu": 30}, covers=["ENV-17"], exception=reason)
    I("budget-allocation", "defaults-above-lowered-ceiling", defaults, context={"cost_ceiling_usd": 40}, covers=["ENV-17"], exception=reason)
    I("budget-allocation", "unregistered-category-typesafe-jev", {**mut(defaults, reserve=DROP), "typesafe_jev": 5}, covers=["ENV-17"], exception=reason)
    I("budget-allocation", "negative-amount", {**defaults, "reserve": -5}, covers=["ENV-17"], exception=reason)
    I("budget-allocation", "empty", {}, covers=["ENV-17"], exception=reason)

    # --------------------------------------------------------- cost estimate
    V("cost-estimate", "cpu-research", COST_CPU, covers=["CS-11", "ENV-17"])
    V("cost-estimate", "gpu", COST_GPU, covers=["CS-11"])
    V("cost-estimate", "phase1-fixture-zero", mut(COST_CPU, estimated_usd_upper_bound=0), covers=["CS-11"])
    for cat in ("platform_infra", "bedrock_explanations", "reserve"):
        V("cost-estimate", f"category-{cat.replace('_', '-')}", mut(COST_CPU, budget_category=cat), covers=["CS-11"])
    I("cost-estimate", "unregistered-category-typesafe", mut(COST_CPU, budget_category="typesafe_jev"), covers=["CS-11", "ENV-17"])
    I("cost-estimate", "negative-estimate", mut(COST_CPU, estimated_usd_upper_bound=-1), covers=["CS-11"])
    I("cost-estimate", "missing-budget-category", mut(COST_CPU, budget_category=DROP), covers=["CS-11"])
    I("cost-estimate", "missing-price-retrieved-at", mut(COST_CPU, price_retrieved_at=DROP), covers=["CS-11"])

    # -------------------------------------------------- cost-allocation tags
    tags = {"project": "finplan", "owner-repo": "financialplanning", "environment": "beta", "logical-role": "snapshot-artifact-bucket", "synthetic": True}
    V("cost-allocation-tags", "platform-bucket", tags, covers=["CS-11"])
    V("cost-allocation-tags", "sagemaker-job", {**tags, "owner-repo": "financemodel", "logical-role": "research-job", "run-id": RUN}, covers=["CS-11"])
    V("cost-allocation-tags", "shared-budget", {**tags, "environment": "shared", "logical-role": "project-budget"}, covers=["CS-11", "ENV-16"])
    I("cost-allocation-tags", "wrong-project", mut(tags, project="other"), covers=["CS-11"])
    I("cost-allocation-tags", "unknown-environment", mut(tags, environment="dev"), covers=["CS-11"])
    I("cost-allocation-tags", "run-id-wrong-prefix", {**tags, "run-id": PV_A}, code="INVALID_IDENTIFIER", covers=["CS-11"])
    I("cost-allocation-tags", "missing-owner-repo", mut(tags, **{"owner-repo": DROP}), covers=["CS-11"])

    # -------------------------------------------------------- job submission
    sub = {**FINANCE, "job_type": "fixture_optimizer", "purpose": "research", "dry_run": False, "input_snapshot_id": SNAP, "configuration": CONFIG, "configuration_id": CFG, "evaluation_window": {"start": "2026-01-02", "end": "2026-01-09"}, "compute_class": "cpu", "max_runtime_seconds": 900, "idempotency_key": "client-key-0002", "contract_version": CV, "synthetic": True}
    V("job-submission", "finance-research-cpu", sub, covers=["DOM-01", "CS-11"])
    V("job-submission", "dry-run", mut(sub, dry_run=True), covers=["CS-11"])
    V("job-submission", "gpu-tuning", mut(sub, purpose="tuning", compute_class="gpu"), covers=["CS-11"])
    I("job-submission", "supply-chain-domain", mut(sub, domain="supply_chain", configuration={**CONFIG, "domain": "supply_chain"}), covers=["DOM-02"])
    I("job-submission", "ticker-in-envelope", mut(sub, ticker=INSTR), covers=["DOM-01"])
    I("job-submission", "finance-payload-invalid", mut(sub, configuration={**CONFIG, "payload": mut(CONFIG_PAYLOAD, universe=DROP)}), covers=["DOM-01"])
    I("job-submission", "s3-input-location", mut(sub, input_location="s3://example-bucket/inputs/"), covers=["CS-08"])
    I("job-submission", "unknown-purpose", mut(sub, purpose="production"), covers=["CS-11"])
    I("job-submission", "missing-idempotency-key", mut(sub, idempotency_key=DROP), covers=["ID-10"])
    I("job-submission", "nested-domain-mismatch", mut(sub, configuration={**CONFIG, "domain": "finance_alt"}), covers=["DOM-02"])

    # ------------------------------------------------------------ job status
    awaiting = mut(JOB_STATUS, run_id=RUN2, state="awaiting_approval", compute_class="gpu", cost_estimate=COST_GPU, transitions=[], elapsed_seconds=0, purpose="tuning")
    V("job-status", "awaiting-approval-gpu", awaiting, covers=["CS-11", "ENV-20"])
    V("job-status", "queued", mut(JOB_STATUS, state="queued"), covers=["CS-11"])
    V("job-status", "running", JOB_STATUS, covers=["CS-11"])
    V("job-status", "stopping", mut(JOB_STATUS, state="stopping"), covers=["CS-11"])
    V("job-status", "approved-gpu-queued", mut(awaiting, state="queued", approval={"approved_by": "synthetic-approver", "approved_at": ts(10, 10, 0), "approved_estimate_usd": 4.5}), covers=["CS-11", "ENV-20"])
    V("job-status", "succeeded", mut(JOB_STATUS, state="succeeded", completion_status="succeeded"), covers=["CS-07", "CS-11"])
    V("job-status", "failed-with-error", mut(JOB_STATUS, state="failed", completion_status="failed", error=err("INTERNAL", False, {"exit": "abnormal"})), covers=["CS-07"])
    V("job-status", "timed-out", mut(JOB_STATUS, state="timed_out", completion_status="timed_out"), covers=["CS-07"])
    I("job-status", "awaiting-approval-with-completion-status", mut(awaiting, completion_status="succeeded"), covers=["CS-11"])
    I("job-status", "terminal-without-completion-status", mut(JOB_STATUS, state="succeeded"), covers=["CS-11"])
    I("job-status", "completion-status-mismatch", mut(JOB_STATUS, state="failed", completion_status="succeeded"), covers=["CS-11"])
    I("job-status", "unknown-state", mut(JOB_STATUS, state="paused"), covers=["CS-11"])
    I("job-status", "unknown-purpose", mut(JOB_STATUS, purpose="production"), covers=["CS-11"])
    I("job-status", "gpu-queued-without-approval", mut(awaiting, state="queued"), covers=["ENV-20"])
    I("job-status", "gpu-left-awaiting-before-approval", mut(awaiting, state="queued", transitions=[{"state": "queued", "at": ts(10, 9, 30)}], approval={"approved_by": "synthetic-approver", "approved_at": ts(10, 10, 0), "approved_estimate_usd": 4.5}), covers=["ENV-20"])
    I("job-status", "gpu-estimate-above-approval", mut(awaiting, state="queued", approval={"approved_by": "synthetic-approver", "approved_at": ts(10, 10, 0), "approved_estimate_usd": 2.0}), covers=["ENV-20"])
    I("job-status", "cost-estimate-unregistered-category",mut(JOB_STATUS, cost_estimate=mut(COST_CPU, budget_category="typesafe_jev")), covers=["CS-11", "ENV-17"])

    # ------------------------------------------------------------ job result
    infeasible = mut(JOB_RESULT, solution_status="infeasible", payload=mut(RUN_PAYLOAD, proposed_allocation=DROP))
    crash = mut(JOB_RESULT, completion_status="failed", solution_status=DROP, error=err("INTERNAL", False, {"exit": "abnormal"}, "job container exited abnormally"), artifacts=[], artifacts_complete=False, payload=DROP)
    V("job-result", "succeeded-optimal", JOB_RESULT, covers=["CS-07"])
    V("job-result", "succeeded-infeasible", infeasible, covers=["CS-07"])
    V("job-result", "succeeded-no-effect", mut(JOB_RESULT, solution_status="no_effect"), covers=["CS-07", "ID-06"])
    V("job-result", "succeeded-unbounded", mut(infeasible, solution_status="unbounded"), covers=["CS-07"])
    V("job-result", "failed-crash", crash, covers=["CS-07"])
    V("job-result", "timed-out-partial-artifacts", mut(JOB_RESULT, completion_status="timed_out", solution_status=DROP, artifacts_complete=False), covers=["CS-07", "ID-07"])
    V("job-result", "cancelled", mut(crash, completion_status="cancelled", error=DROP), covers=["CS-07"])
    I("job-result", "infeasible-reported-as-failure", mut(crash, solution_status="infeasible"), covers=["CS-07"])
    I("job-result", "succeeded-without-solution-status", mut(JOB_RESULT, solution_status=DROP), covers=["CS-07"])
    I("job-result", "failed-without-error", mut(crash, error=DROP), covers=["CS-07"])
    I("job-result", "timed-out-artifacts-complete", mut(JOB_RESULT, completion_status="timed_out", solution_status=DROP, artifacts_complete=True), covers=["CS-07"])
    I("job-result", "succeeded-with-error", mut(JOB_RESULT, error=err("INTERNAL", False)), covers=["CS-07"])
    I("job-result", "unknown-solution-status", mut(JOB_RESULT, solution_status="good"), covers=["CS-07"])
    I("job-result", "payload-missing-sections", mut(JOB_RESULT, payload={"performance": {}}), covers=["DOM-01"])

    # ------------------------------------------------------------ capability
    V("capability", "beta-phase1-model-absent", CAPABILITY, covers=["OWN-06"])
    caps_bad = copy.deepcopy(CAPABILITY)
    caps_bad["tools"][5].pop("unavailable_reason")
    I("capability", "unavailable-without-reason", caps_bad)
    caps_bad2 = copy.deepcopy(CAPABILITY)
    caps_bad2["tools"][0]["input_schema_id"] = sid("core", "tools/not-a-tool-request")
    I("capability", "unresolved-schema-id", caps_bad2)
    I("capability", "missing-release-id", mut(CAPABILITY, release_id=DROP))

    # ------------------------------------------------------- release manifest
    prod = mut(MANIFEST, repo="financialplanning", environment="prod", release_id=REL2, previous_release_id=REL1, artifact_digest=chk("financialplanning-artifact-rel2"),
               outputs={"plan-endpoint": "/finplan/prod/financialplanning/api/plan-endpoint", "registry-ref": "/finplan/shared/financialplanning/contract/registry-ref"},
               approved_by="synthetic-approver", approved_at=ts(16))
    V("release-manifest", "gamma-financelambdastool", MANIFEST, covers=["ENV-06"])
    V("release-manifest", "prod-financialplanning-approved", prod, covers=["ENV-06", "ENV-09"])
    V("release-manifest", "prod-rollback", mut(prod, release_id=REL1, previous_release_id=REL2, rolled_back_from=REL2, artifact_digest=chk("financialplanning-artifact-rel1"), deployed_at=ts(17)), covers=["ENV-06", "ENV-11"])
    V("release-manifest", "beta-financeagent-consumer-only", mut(MANIFEST, repo="financeagent", environment="beta", served_contract_majors=[], previous_release_id=None, outputs={"gateway-principal-ref": "/finplan/beta/financeagent/agent/gateway-principal-ref", "explanation-model-id": "/finplan/beta/financeagent/config/explanation-model-id"}), covers=["ENV-06"])
    I("release-manifest", "prod-without-approval", mut(prod, approved_by=DROP, approved_at=DROP), covers=["ENV-06", "ENV-09"])
    I("release-manifest", "output-in-other-repo-segment", mut(MANIFEST, outputs={"plan-endpoint": f"/finplan/{ENV}/financialplanning/api/plan-endpoint"}), covers=["ENV-07"])
    I("release-manifest", "output-in-other-environment", mut(MANIFEST, outputs={"tool-catalog": "/finplan/prod/financelambdastool/contract/tool-catalog"}), covers=["ENV-07"])
    I("release-manifest", "output-unknown-category", mut(MANIFEST, outputs={"x": f"/finplan/{ENV}/financelambdastool/secrets/x"}), covers=["ENV-07"])
    I("release-manifest", "missing-artifact-digest", mut(MANIFEST, artifact_digest=DROP), covers=["ENV-06"])
    I("release-manifest", "release-id-wrong-prefix", mut(MANIFEST, release_id=RUN), code="INVALID_IDENTIFIER", covers=["ENV-06"])
    I("release-manifest", "environment-shared", mut(MANIFEST, environment="shared"), covers=["ENV-06"])
    I("release-manifest", "missing-served-contract-majors", mut(MANIFEST, served_contract_majors=DROP), covers=["ENV-06", "OWN-08"])

    # ------------------------------------------------- staged-output manifest
    V("staged-output-manifest", "succeeded-optimal", STAGED, covers=["CS-02", "CS-07"])
    V("staged-output-manifest", "succeeded-infeasible-no-files", mut(STAGED, solution_status="infeasible", files=[], payload={"metrics": {"constraint_violation": 0.1}}), covers=["CS-07"])
    V("staged-output-manifest", "failed-partial-output", mut(STAGED, completion_status="failed", solution_status=DROP, files=STAGED["files"][:1], payload=DROP), covers=["CS-07", "ID-07"])
    V("staged-output-manifest", "root-without-parent", mut(STAGED, parent_plan_version_id=None), covers=["ID-06"])
    I("staged-output-manifest", "succeeded-without-solution-status", mut(STAGED, solution_status=DROP), covers=["CS-07"])
    I("staged-output-manifest", "failed-with-solution-status", mut(STAGED, completion_status="failed"), covers=["CS-07"])
    I("staged-output-manifest", "file-path-traversal", mut(STAGED, files=[{"name": "../other-run/plan.json", "checksum": chk("t"), "size_bytes": 1}]), covers=["CS-08"])
    I("staged-output-manifest", "file-without-checksum", mut(STAGED, files=[{"name": "plan-content.json", "size_bytes": 1}]))
    I("staged-output-manifest", "missing-run-id", mut(STAGED, run_id=DROP))
    I("staged-output-manifest", "model-version-wrong-prefix", mut(STAGED, model_version=RUN), code="INVALID_IDENTIFIER", covers=["ID-01"])

    # ---------------------------------------------------------- import report
    rejected = mut(IMPORT_ACCEPTED, outcome="rejected", created_plan_version_id=DROP, no_effect=DROP, findings=[{"severity": "error", "code": "VALIDATION_FAILED", "message": "instrument not in the version's snapshot", "sheet": "allocations", "row": 4, "column": "A", "field": "instrument_id", "details": {"instrument_id": "SYNTH-UNKNOWN"}}])
    V("import-report", "accepted", IMPORT_ACCEPTED, covers=["ID-06"])
    V("import-report", "accepted-no-effect", mut(IMPORT_ACCEPTED, created_plan_version_id=PV_D, no_effect=True), covers=["ID-06"])
    V("import-report", "rejected-unknown-instrument", rejected)
    V("import-report", "rejected-stale-workbook", mut(rejected, findings=[{"severity": "error", "code": "CONFLICT", "message": "workbook expected_revision is stale", "details": {"current_version_id": PV_B, "current_revision": 3}}]), covers=["ID-11"])
    I("import-report", "accepted-without-created-version", mut(IMPORT_ACCEPTED, created_plan_version_id=DROP))
    I("import-report", "rejected-without-error-finding", mut(rejected, findings=[{"severity": "warning", "code": "VALIDATION_FAILED", "message": "synthetic warning"}]))
    I("import-report", "rejected-with-created-version", mut(rejected, created_plan_version_id=PV_C))
    I("import-report", "source-not-excel", mut(IMPORT_ACCEPTED, source_artifact=aref("plan_export", 43)))
    I("import-report", "bad-template-version", mut(IMPORT_ACCEPTED, template_version="xlsm-plan-v1"))

    # ----------------------------------------------------------- tool catalog
    V("tool-catalog", "gamma-catalog", CATALOG, covers=["CS-02"])
    cat = copy.deepcopy(CATALOG)
    cat["tools"][0]["lambda_ref_parameter"] = "/finplan/prod/financelambdastool/lambda/describe-capabilities-arn"
    I("tool-catalog", "lambda-ref-other-environment", cat, covers=["ENV-03"])
    cat = copy.deepcopy(CATALOG)
    cat["tools"][1]["output_schema_id"] = sid("finance", "tools/query-market-data-v2-response")
    I("tool-catalog", "unresolved-schema-id", cat)
    cat = copy.deepcopy(CATALOG)
    cat["tools"][2]["role_class"] = "admin"
    I("tool-catalog", "unknown-role-class", cat)
    cat = copy.deepcopy(CATALOG)
    cat["tools"].append(copy.deepcopy(cat["tools"][0]))
    I("tool-catalog", "duplicate-tool", cat)
    cat = copy.deepcopy(CATALOG)
    cat["tools"][3].pop("lambda_ref_parameter")
    I("tool-catalog", "missing-lambda-ref", cat)

    # ------------------------------------------------------------ explanations
    subject = {"plan_version_id": PV_C, "compare_to_plan_version_id": PV_A}
    ev = {**aref("explanation_evidence", 61, "financemodel"), "domain": "finance"}
    ereq = {**FINANCE, "subject": subject, "evidence": [ev], "question": "Why did the allocation change?", "idempotency_key": "client-key-0003", "contract_version": CV, "synthetic": True}
    V("explanation-request", "compare-versions", ereq, covers=["DOM-04"])
    I("explanation-request", "supply-chain-domain", mut(ereq, domain="supply_chain"), covers=["DOM-02"])
    I("explanation-request", "evidence-wrong-kind", mut(ereq, evidence=[aref("plan_content", 62)]), covers=["DOM-04"])
    I("explanation-request", "missing-idempotency-key", mut(ereq, idempotency_key=DROP), covers=["ID-10"])
    eres = {**FINANCE, "subject": subject, "evidence": [ev], "narrative": "The synthetic allocation moved toward cash; see the evidence artifact for the decomposition.", "generated_at": ts(18), "synthetic": True}
    V("explanation-result", "evidence-and-narrative", eres, covers=["DOM-04"])
    I("explanation-result", "narrative-only", mut(eres, evidence=[]), covers=["DOM-04"])
    I("explanation-result", "evidence-without-checksum", mut(eres, evidence=[mut(ev, checksum=DROP)]), covers=["DOM-04"])
    I("explanation-result", "missing-narrative", mut(eres, narrative=DROP), covers=["DOM-04"])

    # ------------------------------------------------------------------ tools
    T = "tools/"
    V(T + "describe-capabilities-request", "empty", {"synthetic": True})
    V(T + "describe-capabilities-request", "with-contract-version", {"contract_version": CV, "synthetic": True})
    I(T + "describe-capabilities-request", "unknown-field", {"verbose": True, "synthetic": True})
    V(T + "describe-capabilities-response", "beta-phase1", CAPABILITY, covers=["OWN-06"])
    I(T + "describe-capabilities-response", "missing-tools", mut(CAPABILITY, tools=DROP))

    V(T + "get-plan-request", "by-plan-id", {"plan_id": PL, "synthetic": True})
    I(T + "get-plan-request", "wrong-prefix", {"plan_id": PV_A, "synthetic": True}, code="INVALID_IDENTIFIER", covers=["ID-01"])
    I(T + "get-plan-request", "storage-location-field", {"plan_id": PL, "location": "s3://example-bucket/plan.json", "synthetic": True}, covers=["CS-08"])
    V(T + "get-plan-response", "with-publication", {"plan": plan, "current_publication": PUBLICATION, "synthetic": True})
    V(T + "get-plan-response", "without-publication", {"plan": mut(plan, current_publication_id=None), "current_publication": None, "synthetic": True})
    I(T + "get-plan-response", "missing-plan", {"current_publication": None, "synthetic": True})

    V(T + "get-plan-version-request", "by-id", {"plan_version_id": PV_A, "synthetic": True})
    I(T + "get-plan-version-request", "wrong-prefix", {"plan_version_id": PL, "synthetic": True}, code="INVALID_IDENTIFIER", covers=["ID-01"])
    V(T + "get-plan-version-response", "compact", {"plan_version": mut(PV_ROOT, content=DROP), "content_summary": {"top_weights": [{"instrument_id": INSTR, "weight": 0.6}], "other_weight": 0.4}, "content_ref": aref("plan_content", 21), "truncated": False, "synthetic": True})
    I(T + "get-plan-version-response", "plan-version-missing-status", {"plan_version": mut(PV_ROOT, status=DROP), "synthetic": True})

    V(T + "list-plan-versions-request", "first-page", {"plan_id": PL, "page_size": 2, "synthetic": True})
    V(T + "list-plan-versions-request", "next-page", {"plan_id": PL, "page_size": 2, "next_token": "eyJwYWdlIjoyfQ", "synthetic": True})
    I(T + "list-plan-versions-request", "page-size-zero", {"plan_id": PL, "page_size": 0, "synthetic": True})
    I(T + "list-plan-versions-request", "token-is-a-storage-key", {"plan_id": PL, "next_token": "example-bucket/plans/page2", "synthetic": True}, covers=["CS-08"])
    entry = lambda pv: {k: pv[k] for k in ("plan_version_id", "parent_plan_version_id", "origin", "status", "checksum", "created_at")}  # noqa: E731
    V(T + "list-plan-versions-response", "page-with-token", {"plan_id": PL, "versions": [entry(excel), entry(child)], "next_token": "eyJwYWdlIjoyfQ", "synthetic": True})
    V(T + "list-plan-versions-response", "last-page", {"plan_id": PL, "versions": [entry(PV_ROOT)], "next_token": None, "synthetic": True})
    I(T + "list-plan-versions-response", "missing-next-token", {"plan_id": PL, "versions": [], "synthetic": True})

    V(T + "get-job-status-request", "by-run-id", {"run_id": RUN, "synthetic": True})
    I(T + "get-job-status-request", "wrong-prefix", {"run_id": MV, "synthetic": True}, code="INVALID_IDENTIFIER", covers=["ID-01"])
    V(T + "get-job-status-response", "awaiting-approval", awaiting, covers=["CS-11"])
    I(T + "get-job-status-response", "awaiting-with-completion-status", mut(awaiting, completion_status="succeeded"), covers=["CS-11"])
    I(T + "get-job-status-response", "gpu-running-without-approval", mut(awaiting, state="running"), covers=["ENV-20"])

    V(T + "get-experiment-result-request", "by-run-id", {"run_id": RUN, "synthetic": True})
    I(T + "get-experiment-result-request", "missing-run-id", {"synthetic": True})
    V(T + "get-experiment-result-response", "infeasible", infeasible, covers=["CS-07"])
    I(T + "get-experiment-result-response", "failed-without-error", mut(crash, error=DROP), covers=["CS-07"])

    sreq = {**FINANCE, "job_type": "fixture_optimizer", "purpose": "research", "dry_run": False, "input_snapshot_id": SNAP, "configuration": CONFIG, "evaluation_window": {"start": "2026-01-02", "end": "2026-01-09"}, "idempotency_key": "client-key-0004", "contract_version": CV, "synthetic": True}
    V(T + "submit-experiment-request", "research", sreq, covers=["DOM-01"])
    V(T + "submit-experiment-request", "dry-run", mut(sreq, dry_run=True))
    I(T + "submit-experiment-request", "s3-configuration-location", mut(sreq, configuration_location="s3://example-bucket/config.json"), covers=["CS-08"])
    I(T + "submit-experiment-request", "supply-chain-domain", mut(sreq, domain="supply_chain", configuration={**CONFIG, "domain": "supply_chain"}), covers=["DOM-02"])
    I(T + "submit-experiment-request", "missing-idempotency-key", mut(sreq, idempotency_key=DROP), covers=["ID-10"])
    I(T + "submit-experiment-request", "path-in-job-type", mut(sreq, job_type="../fixture"), covers=["CS-08"])
    sresp = {"run_id": RUN2, "configuration_id": CFG, "state": "awaiting_approval", "dry_run": False, "cost_estimate": COST_GPU, "message": "A human approver must approve this run.", "synthetic": True}
    V(T + "submit-experiment-response", "awaiting-approval-gpu", sresp, covers=["CS-11", "ENV-20"])
    V(T + "submit-experiment-response", "queued-cpu", mut(sresp, state="queued", cost_estimate=COST_CPU), covers=["CS-11"])
    V(T + "submit-experiment-response", "dry-run-estimate", {"run_id": None, "configuration_id": CFG, "state": None, "dry_run": True, "cost_estimate": COST_CPU, "synthetic": True}, covers=["CS-11"])
    I(T + "submit-experiment-response", "submitted-without-run-id", mut(sresp, run_id=None), code="INVALID_IDENTIFIER", covers=["CS-11"])
    I(T + "submit-experiment-response", "dry-run-with-run-id", mut(sresp, dry_run=True), code="VALIDATION_FAILED", covers=["CS-11"])
    I(T + "submit-experiment-response", "cost-unregistered-category", mut(sresp, cost_estimate=mut(COST_GPU, budget_category="gpu_burst")), covers=["CS-11"])

    oreq = {"plan_id": PL, "parent_plan_version_id": PV_A, "expected_revision": 4, "idempotency_key": "client-key-0005", **FINANCE, "content": PLAN_CONTENT, "reason": "synthetic override", "synthetic": True}
    V(T + "create-override-version-request", "override", oreq, covers=["ID-06", "ID-11"])
    I(T + "create-override-version-request", "missing-expected-revision", mut(oreq, expected_revision=DROP), covers=["ID-11"])
    I(T + "create-override-version-request", "content-invalid-finance-payload", mut(oreq, content={"base_currency": "USD"}), covers=["DOM-01"])
    I(T + "create-override-version-request", "client-supplied-plan-version-id", mut(oreq, plan_version_id=PV_D), covers=["ID-03"])
    I(T + "create-override-version-request", "content-location-uri", mut(oreq, content_location="s3://example-bucket/content.json"), covers=["CS-08"])
    oresp = {"plan_version_id": PV_B, "parent_plan_version_id": PV_A, "origin": "manual_override", "status": "pending_validation", "checksum": chk("plan-content-B"), "no_effect": False, "revision": 5, "synthetic": True}
    V(T + "create-override-version-response", "child-created", oresp, covers=["ID-06"])
    V(T + "create-override-version-response", "no-effect-child", mut(oresp, plan_version_id=PV_D, checksum=PV_ROOT["checksum"], no_effect=True), covers=["ID-06"])
    I(T + "create-override-version-response", "wrong-origin", mut(oresp, origin="excel_import"), covers=["ID-06"])
    I(T + "create-override-version-response", "missing-no-effect", mut(oresp, no_effect=DROP), covers=["ID-06"])

    V(T + "validate-plan-version-request", "validate", {"plan_version_id": PV_B, "idempotency_key": "client-key-0006", "synthetic": True}, covers=["ID-07"])
    I(T + "validate-plan-version-request", "missing-idempotency-key", {"plan_version_id": PV_B, "synthetic": True}, covers=["ID-10"])
    V(T + "validate-plan-version-response", "validated", {"plan_version_id": PV_B, "status": "validated", "findings": [], "synthetic": True}, covers=["ID-07"])
    V(T + "validate-plan-version-response", "invalid-reconciliation", {"plan_version_id": PV_B, "status": "invalid", "findings": [{"code": "VALIDATION_FAILED", "message": "weights plus cash do not sum to 1", "pointer": "/allocation"}], "synthetic": True}, covers=["ID-07"])
    I(T + "validate-plan-version-response", "invalid-without-findings", {"plan_version_id": PV_B, "status": "invalid", "findings": [], "synthetic": True}, covers=["ID-07"])
    I(T + "validate-plan-version-response", "pending-is-not-a-result", {"plan_version_id": PV_B, "status": "pending_validation", "findings": [], "synthetic": True}, covers=["ID-07"])

    preq = {"plan_id": PL, "plan_version_id": PV_A, "expected_revision": 2, "idempotency_key": "client-key-0007", "synthetic": True}
    V(T + "publish-plan-version-request", "publish", preq, covers=["ID-08"])
    I(T + "publish-plan-version-request", "mode-live", mut(preq, mode="live"), covers=["ID-09"])
    I(T + "publish-plan-version-request", "execute-flag", mut(preq, execute=True), covers=["ID-09"])
    I(T + "publish-plan-version-request", "missing-expected-revision", mut(preq, expected_revision=DROP), covers=["ID-11"])
    V(T + "publish-plan-version-response", "published", PUBLICATION, covers=["ID-08"])
    I(T + "publish-plan-version-response", "missing-publication-id", mut(PUBLICATION, publication_id=DROP), covers=["ID-08"])

    qreq = {"input_snapshot_id": SNAP, "instrument_ids": [INSTR], "start_date": "2026-01-02", "end_date": "2026-01-09", "page_size": 50, "synthetic": True}
    V(T + "query-market-data-request", "etf-daily", qreq, covers=["DOM-03"])
    I(T + "query-market-data-request", "storage-uri-field", mut(qreq, source="s3://example-bucket/snap/"), covers=["CS-08"])
    I(T + "query-market-data-request", "wrong-prefix", mut(qreq, input_snapshot_id=PV_A), code="INVALID_IDENTIFIER", covers=["ID-01"])
    qresp = {"snapshot": SNAPSHOT, "observation_kinds": ["completed_daily"], "instruments": [{"instrument_id": INSTR, "observation_count": 6, "first_date": "2026-01-02", "last_date": "2026-01-09", "close_min": 100.0, "close_max": 103.0, "close_last": 102.0}], "partial": False, "data_refs": [aref("snapshot_payload", 12)], "next_token": None, "synthetic": True}
    V(T + "query-market-data-response", "approved-full-coverage", qresp, covers=["DOM-03", "ID-05"])
    V(T + "query-market-data-response", "partial-missing-sessions", mut(qresp, snapshot=mut(SNAPSHOT, quality_flags=["missing_sessions"]), partial=True), covers=["CS-11"])
    I(T + "query-market-data-response", "missing-partial", mut(qresp, partial=DROP))
    I(T + "query-market-data-response", "unknown-observation-kind", mut(qresp, observation_kinds=["weekly"]), covers=["DOM-03"])

    rreq = {"dataset_id": DATASET, "instrument_ids": [INSTR], "start_date": "2026-01-02", "end_date": "2026-01-09", "granularity": "daily", "idempotency_key": "client-key-0008", "synthetic": True}
    V(T + "refresh-market-data-request", "daily", rreq, covers=["DOM-03", "ID-10"])
    I(T + "refresh-market-data-request", "intraday-granularity", mut(rreq, granularity="intraday"), covers=["DOM-03"])
    I(T + "refresh-market-data-request", "missing-idempotency-key", mut(rreq, idempotency_key=DROP), covers=["ID-10"])
    I(T + "refresh-market-data-request", "provider-url", mut(rreq, provider_url="https://example.invalid/quote"), covers=["CS-08"])
    rresp = {"snapshot": mut(SNAPSHOT, status="committed", approval_rule_version=DROP), "requested_range": {"start": "2026-01-02", "end": "2026-01-09"}, "coverage_complete": True, "synthetic": True}
    V(T + "refresh-market-data-response", "complete", rresp, covers=["ID-05"])
    V(T + "refresh-market-data-response", "partial-response-flag", mut(rresp, snapshot=mut(SNAPSHOT, status="committed", approval_rule_version=DROP, quality_flags=["partial_response"], coverage={"start": "2026-01-02", "end": "2026-01-07"}), coverage_complete=False), covers=["CS-11"])
    I(T + "refresh-market-data-response", "missing-coverage-complete", mut(rresp, coverage_complete=DROP))
    platform_rresp = {**rresp, "input_snapshot_id": SNAP, "new_snapshot": True, "content_checksum": chk("snapshot-payload-1"), "quality_flags": [], "trigger": "on_demand"}
    V(T + "refresh-market-data-response", "platform-fields", platform_rresp, covers=["ID-05", "ID-10"])
    holiday = {"input_snapshot_id": SNAP, "requested_range": {"start": "2026-01-19", "end": "2026-01-19"}, "coverage_complete": False, "new_snapshot": False, "quality_flags": ["no_session"], "session_status": "holiday", "trigger": "scheduled", "snapshot": SNAPSHOT, "synthetic": True}
    V(T + "refresh-market-data-response", "no-session-holiday-latest-snapshot", holiday, covers=["CS-11"])
    V(T + "refresh-market-data-response", "no-session-holiday-no-snapshot-yet", mut(holiday, snapshot=DROP, input_snapshot_id=None), covers=["CS-11"])
    I(T + "refresh-market-data-response", "no-snapshot-without-no-session-flag", mut(holiday, snapshot=DROP, input_snapshot_id=None, quality_flags=["stale_source"]), covers=["CS-11"])
    I(T + "refresh-market-data-response", "no-snapshot-but-new-snapshot", mut(holiday, snapshot=DROP, input_snapshot_id=None, new_snapshot=True), covers=["CS-11"])
    I(T + "refresh-market-data-response", "no-snapshot-but-snapshot-id", mut(holiday, snapshot=DROP), covers=["CS-11"])

    # ------------------------------------------------- plan lifecycle API routes (0.2.0, design D13)
    A = "api/"
    pf_req = {"name": "Synthetic smoke portfolio", "base_currency": "USD", "synthetic": True, "settings": {"risk_profile": "moderate"}, "idempotency_key": "client-key-0010", "contract_version": CV}
    V(A + "create-portfolio-request", "synthetic-portfolio", pf_req, covers=["ID-10"])
    I(A + "create-portfolio-request", "client-supplied-portfolio-id", {**pf_req, "portfolio_id": PF}, covers=["ID-03"])
    I(A + "create-portfolio-request", "missing-idempotency-key", mut(pf_req, idempotency_key=DROP), covers=["ID-10"])
    I(A + "create-portfolio-request", "lowercase-currency", mut(pf_req, base_currency="usd"))
    pf_doc = {"portfolio_id": PF, "name": "Synthetic smoke portfolio", "base_currency": "USD", "synthetic": True, "settings": {"risk_profile": "moderate"}, "revision": 1, "created_at": ts(1)}
    V(A + "create-portfolio-response", "created", {**pf_doc, "contract_version": CV})
    I(A + "create-portfolio-response", "missing-contract-version", pf_doc, covers=["CS-05"])
    I(A + "create-portfolio-response", "wrong-prefix", {**pf_doc, "portfolio_id": PL, "contract_version": CV}, code="INVALID_IDENTIFIER", covers=["ID-01"])

    pl_req = {"portfolio_id": PF, "name": "Synthetic plan", "expected_revision": 1, "idempotency_key": "client-key-0011", "contract_version": CV, "synthetic": True}
    V(A + "create-plan-request", "plan", pl_req, covers=["ID-11"])
    I(A + "create-plan-request", "missing-expected-revision", mut(pl_req, expected_revision=DROP), covers=["ID-11"])
    I(A + "create-plan-request", "client-supplied-plan-id", {**pl_req, "plan_id": PL}, covers=["ID-03"])
    I(A + "create-plan-request", "portfolio-id-wrong-prefix", mut(pl_req, portfolio_id=PL), code="INVALID_IDENTIFIER", covers=["ID-01"])
    new_plan = mut(plan, head={"current_version_id": None, "revision": 1}, current_publication_id=None)
    V(A + "create-plan-response", "created", {**new_plan, "publication_revision": 1, "portfolio_revision": 2, "contract_version": CV}, covers=["ID-11"])
    I(A + "create-plan-response", "missing-publication-revision", {**new_plan, "portfolio_revision": 2, "contract_version": CV}, covers=["ID-11"])
    I(A + "create-plan-response", "missing-head", {**mut(new_plan, head=DROP), "publication_revision": 1, "portfolio_revision": 2})

    root_req = {"plan_id": PL, "expected_revision": 1, "idempotency_key": "client-key-0012", "origin": "model_run", "input_snapshot_id": SNAP, "configuration_id": CFG, "model_version": MV, "run_id": RUN, **FINANCE, "content": PLAN_CONTENT, "contract_version": CV, "synthetic": True}
    V(A + "create-root-version-request", "model-run-root", root_req, covers=["ID-06"])
    V(A + "create-root-version-request", "explicit-null-parent", {**root_req, "parent_plan_version_id": None}, covers=["ID-06"])
    I(A + "create-root-version-request", "with-parent", {**root_req, "parent_plan_version_id": PV_A}, covers=["ID-06"])
    I(A + "create-root-version-request", "manual-override-origin", mut(root_req, origin="manual_override"), covers=["ID-06"])
    I(A + "create-root-version-request", "missing-run-id", mut(root_req, run_id=DROP), covers=["ID-06"])
    I(A + "create-root-version-request", "content-invalid-finance-payload", mut(root_req, content={"base_currency": "USD"}), covers=["DOM-01"])
    I(A + "create-root-version-request", "content-location-uri", {**root_req, "content_location": "s3://example-bucket/content.json"}, covers=["CS-08"])
    I(A + "create-root-version-request", "client-supplied-plan-version-id", {**root_req, "plan_version_id": PV_D}, covers=["ID-03"])
    root_resp = {"plan_version_id": PV_A, "plan_id": PL, "parent_plan_version_id": None, "origin": "model_run", "status": "pending_validation", "checksum": chk("plan-content-A"), "no_effect": False, "revision": 2, "input_snapshot_id": SNAP, "content_ref": aref("plan_content", 21), "contract_version": CV, "synthetic": True}
    V(A + "create-root-version-response", "root-created", root_resp, covers=["ID-06"])
    V(A + "create-root-version-response", "model-run-child", mut(root_resp, plan_version_id=PV_D, parent_plan_version_id=PV_A, revision=3, content_ref=aref("plan_content", 24)), covers=["ID-06"])
    I(A + "create-root-version-response", "missing-content-ref", mut(root_resp, content_ref=DROP), covers=["CS-08"])
    I(A + "create-root-version-response", "unknown-status", mut(root_resp, status="published"), covers=["ID-07"])

    ex_req = {"publication_id": PUB, "mode": "paper", "note": "synthetic paper execution", "idempotency_key": "client-key-0013", "contract_version": CV, "synthetic": True}
    V(A + "record-execution-request", "paper", ex_req, covers=["ID-09"])
    V(A + "record-execution-request", "simulated", mut(ex_req, mode="simulated"), covers=["ID-09"])
    I(A + "record-execution-request", "live-mode", mut(ex_req, mode="live"), code="OPERATION_NOT_PERMITTED", covers=["ID-09"])
    I(A + "record-execution-request", "missing-idempotency-key", mut(ex_req, idempotency_key=DROP), covers=["ID-10"])
    I(A + "record-execution-request", "broker-account-field", {**ex_req, "account": "synthetic-account"}, covers=["ID-09"])
    V(A + "record-execution-response", "paper-recorded", {**exe, "contract_version": CV}, covers=["ID-09"])
    I(A + "record-execution-response", "live-mode", {**mut(exe, mode="live"), "contract_version": CV}, code="OPERATION_NOT_PERMITTED", covers=["ID-09"])
    I(A + "record-execution-response", "missing-contract-version", exe, covers=["CS-05"])

    obs_page = [obs_fixture(d, c) for d, c in (("2026-01-02", 101.0), ("2026-01-05", 102.0))]
    oresp_full = {**qresp, "observations": obs_page, "requested_range": {"start": "2026-01-02", "end": "2026-01-09"}, "uncovered_ranges": [], "missing_sessions": []}
    V(A + "read-snapshot-observations-response", "first-page", oresp_full, covers=["DOM-03", "ID-05"])
    V(A + "read-snapshot-observations-response", "uncovered-range-stated", mut(oresp_full, requested_range={"start": "2026-01-02", "end": "2026-01-16"}, uncovered_ranges=[{"start": "2026-01-10", "end": "2026-01-16"}], missing_sessions=["2026-01-06"], partial=True), covers=["CS-11"])
    I(A + "read-snapshot-observations-response", "missing-observations", mut(oresp_full, observations=DROP))
    I(A + "read-snapshot-observations-response", "observation-unknown-kind", mut(oresp_full, observations=[{**obs_page[0], "kind": "weekly"}]), covers=["DOM-03"])
    I(A + "read-snapshot-observations-response", "missing-partial", mut(oresp_full, partial=DROP))

    so_base = {"run_id": RUN, "plan_id": PL, "decided_at": ts(10, 9, 40), "correlation_id": CORR, "model_version": MV, "configuration_id": CFG, "input_snapshot_id": SNAP, "parent_plan_version_id": PV_A, "completion_status": "succeeded", "manifest_checksum": chk("staged-manifest-1"), "contract_version": CV, "synthetic": True}
    accepted = {**so_base, "outcome": "accepted", "solution_status": "optimal", "plan_version_id": PV_C, "manifest_ref": aref("staged_output_manifest", 71), "plan_version_status": "validated", "checksum": chk("plan-content-C")}
    V(A + "get-staged-output-response", "accepted", accepted, covers=["CS-07", "ID-06"])
    V(A + "get-staged-output-response", "no-version-infeasible", {**so_base, "outcome": "no_version", "solution_status": "infeasible"}, covers=["CS-07"])
    run_failed = err("PRECONDITION_FAILED", False, {"reason": "run_not_succeeded", "completion_status": "failed", "run_id": RUN2})
    V(A + "get-staged-output-response", "rejected-run-outcome", {**mut(so_base, run_id=RUN2, completion_status="failed"), "outcome": "rejected", "rejection_kind": "run_outcome", "error": run_failed}, covers=["CS-07", "ID-07"])
    structural = err("VALIDATION_FAILED", False, {"pointer": "/files", "problems": ["checksum_mismatch"]}, "staged files do not match the manifest")
    V(A + "get-staged-output-response", "rejected-structural", {**mut(so_base, run_id=RUN3), "outcome": "rejected", "solution_status": "optimal", "rejection_kind": "structural", "error": structural, "rejected_files": [{"name": "metrics/summary.json", "problem": "checksum_mismatch"}]}, covers=["CS-08", "ID-07"])
    I(A + "get-staged-output-response", "accepted-without-plan-version", mut(accepted, plan_version_id=DROP), covers=["ID-06"])
    I(A + "get-staged-output-response", "rejected-without-error", {**so_base, "outcome": "rejected", "rejection_kind": "run_outcome"}, covers=["CS-05"])
    I(A + "get-staged-output-response", "rejected-with-plan-version", {**so_base, "outcome": "rejected", "rejection_kind": "run_outcome", "error": run_failed, "plan_version_id": PV_C}, covers=["ID-06"])
    I(A + "get-staged-output-response", "no-version-optimal", {**so_base, "outcome": "no_version", "solution_status": "optimal"}, covers=["CS-07"])
    I(A + "get-staged-output-response", "unknown-outcome", mut(accepted, outcome="pending"))
    I(A + "get-staged-output-response", "rejected-file-path-traversal", {**mut(so_base, run_id=RUN3), "outcome": "rejected", "rejection_kind": "structural", "error": structural, "rejected_files": [{"name": "../other-run/plan.json", "problem": "unlisted"}]}, covers=["CS-08"])
    I(A + "get-staged-output-response", "rejection-kind-not-vocabulary", {**mut(so_base, run_id=RUN3), "outcome": "rejected", "rejection_kind": "Structural Error", "error": structural}, covers=["CS-11"])
    I(A + "get-staged-output-response", "manifest-ref-s3-uri", mut(accepted, manifest_ref={**aref("staged_output_manifest", 72), "artifact_id": "s3://example-bucket/staging/run/manifest.json"}), covers=["CS-08"])

    # ---------------------------------------------------------------- finance
    instrument = {"instrument_id": INSTR, "name": "Synthetic S&P 500 tracking ETF series", "asset_class": "etf", "currency": "USD", "exchange_mic": "XNYS", "synthetic": True}
    V("instrument", "etf", instrument, covers=["DOM-03"])
    V("instrument", "cash", {"instrument_id": "CASH", "asset_class": "cash", "currency": "USD", "synthetic": True}, covers=["DOM-03"])
    I("instrument", "lowercase-id", mut(instrument, instrument_id="spy"))
    I("instrument", "missing-currency", mut(instrument, currency=DROP))

    alloc = {"weights": [{"instrument_id": INSTR, "weight": 0.6}], "cash_weight": 0.4, "synthetic": True}
    V("allocation", "sixty-forty", alloc, covers=["DOM-03"])
    I("allocation", "weight-above-one", mut(alloc, weights=[{"instrument_id": INSTR, "weight": 1.5}]))
    I("allocation", "empty-weights", mut(alloc, weights=[]))

    V("constraints", "long-only", {"long_only": True, "max_weight": 0.8, "min_weight": 0.0, "max_turnover": 0.5, "synthetic": True}, covers=["DOM-03"])
    I("constraints", "max-weight-above-one", {"max_weight": 1.5, "synthetic": True})

    V("fees", "bps", {"transaction_cost_bps": 5, "management_fee_bps": 9, "synthetic": True}, covers=["DOM-03"])
    I("fees", "negative-cost", {"transaction_cost_bps": -1, "synthetic": True})

    V("plan-content", "sixty-forty", {**PLAN_CONTENT, "synthetic": True}, covers=["DOM-03"])
    I("plan-content", "missing-allocation", {"base_currency": "USD", "synthetic": True})

    obs = {"instrument_id": INSTR, "session_date": "2026-01-02", "kind": "completed_daily", "session_status": "regular", "open": 100.0, "high": 101.5, "low": 99.5, "close": 101.0, "volume": 1000, "adj_close": 100.8, "dividend": 0.0, "split_ratio": 1.0, "synthetic": True}
    V("observation", "completed-daily", obs, covers=["DOM-03", "CS-11"])
    V("observation", "schema-upgrade-without-optional-fields", {"instrument_id": INSTR, "session_date": "2026-01-05", "kind": "completed_daily", "close": 102.0, "synthetic": True}, covers=["DOM-03", "CS-03"])
    V("observation", "intraday-partial", mut(obs, kind="intraday_partial", adj_close=DROP, dividend=DROP, split_ratio=DROP), covers=["DOM-03"])
    I("observation", "unknown-kind", mut(obs, kind="weekly"), covers=["DOM-03"])
    I("observation", "zero-split-ratio", mut(obs, split_ratio=0), covers=["CS-11"])
    I("observation", "negative-dividend", mut(obs, dividend=-0.1), covers=["CS-11"])
    I("observation", "missing-close", mut(obs, close=DROP), covers=["DOM-03"])

    spay = {"dataset_id": DATASET, "calendar": "XNYS", "instruments": [instrument], "observations": [obs, mut(obs, session_date="2026-01-05", close=102.0, open=101.0, high=102.5, low=100.5, adj_close=101.8)], "synthetic": True}
    V("snapshot-payload", "etf-daily", spay, covers=["DOM-03"])
    I("snapshot-payload", "no-instruments", mut(spay, instruments=[]), covers=["DOM-03"])
    I("snapshot-payload", "dataset-not-finance", mut(spay, dataset_id="supply_chain/shipments/x"), covers=["DOM-03"])

    V("experiment-config", "mean-variance", {**CONFIG_PAYLOAD, "synthetic": True}, covers=["DOM-03"])
    I("experiment-config", "unknown-field", {**CONFIG_PAYLOAD, "leverage": 3, "synthetic": True})
    I("experiment-config", "missing-universe", mut({**CONFIG_PAYLOAD, "synthetic": True}, universe=DROP))

    V("run-result-payload", "sections", {**RUN_PAYLOAD, "synthetic": True}, covers=["DOM-03"])
    I("run-result-payload", "missing-accuracy-section", mut({**RUN_PAYLOAD, "synthetic": True}, accuracy=DROP))

    V("staged-output-payload", "plan-content", {"plan_content": PLAN_CONTENT, "metrics": {"expected_return": 0.05}, "synthetic": True}, covers=["DOM-03"])
    I("staged-output-payload", "weight-above-one", {"plan_content": mut(PLAN_CONTENT, allocation={"weights": [{"instrument_id": INSTR, "weight": 2}]}), "synthetic": True})

    evid = {"evidence_kind": "allocation_delta", "subject": subject, "metrics": [{"name": "weight_change_spy", "value": -0.1, "unit": "fraction"}], "source_refs": [aref("plan_content", 21), aref("plan_content", 23)], "synthetic": True}
    V("explanation-evidence", "allocation-delta", evid, covers=["DOM-04"])
    I("explanation-evidence", "no-metrics", mut(evid, metrics=[]), covers=["DOM-04"])
    I("explanation-evidence", "metric-not-numeric", mut(evid, metrics=[{"name": "weight_change_spy", "value": "down"}]), covers=["DOM-04"])

    tmpl = {"template_version": "xlsx-plan-v1", "metadata": {"plan_id": PL, "base_plan_version_id": PV_A, "base_checksum": chk("plan-content-A"), "expected_revision": 2, "exported_at": ts(12)}, "allocation_rows": [{"row": 2, "instrument_id": INSTR, "target_weight": 0.6, "formula_tolerant": False}, {"row": 3, "instrument_id": "CASH", "target_weight": 0.4}], "synthetic": True}
    V("excel-plan-template", "xlsx-plan-v1", tmpl, covers=["ID-06"])
    I("excel-plan-template", "unknown-template-version", mut(tmpl, template_version="xlsx-plan-v2"))
    I("excel-plan-template", "header-row-edited", mut(tmpl, allocation_rows=[{"row": 1, "instrument_id": INSTR, "target_weight": 0.6}]))
    I("excel-plan-template", "metadata-missing-revision", mut(tmpl, metadata=mut(tmpl["metadata"], expected_revision=DROP)), covers=["ID-11"])

    pf = {"portfolio_id": PF, "name": "Synthetic smoke portfolio", "base_currency": "USD", "synthetic": True, "settings": {"risk_profile": "moderate"}, "revision": 1, "created_at": ts(1)}
    V("portfolio", "synthetic-portfolio", pf)
    I("portfolio", "missing-synthetic-flag", mut(pf, synthetic=DROP), exception="the portfolio's own 'synthetic' field is the property under test, so this invalid fixture omits it")
    I("portfolio", "wrong-prefix", mut(pf, portfolio_id=PL), code="INVALID_IDENTIFIER", covers=["ID-01"])


# ------------------------------------------------------------------- output
README_HEAD = """# Contract fixtures

Deterministic, synthetic fixtures for every contract schema, generated by
`contracts/python/scripts/gen_fixtures.py` (do not edit the JSON by hand; edit the
generator and rerun it). Layout:

- `<schema-name>/valid/*.json` must validate against the schema (including its
  semantic checks);
- `<schema-name>/invalid/*.json` must fail; the expected error code and any
  check context (for example `cost_ceiling_usd`) are in
  `contracts/conformance/cases.yaml`;
- `vectors/*.json` are cross-language test vectors (configuration IDs, proxied
  idempotency keys), generated by `contracts/python/scripts/gen_vectors.py`.

`<schema-name>` is the schema path below `<namespace>/v<major>/` without `.json`
(for example `plan-version`, `tools/get-plan-request`).

All values are synthetic: identifiers come from a fixed timestamp and a counter,
checksums are SHA-256 of labels, prices are made-up round numbers, lineage names
the `fixture`/`mock` provider, and no fixture contains real holdings, account
exports, credentials, account identifiers or live market values. Example storage
URIs in invalid fixtures use the placeholder bucket `example-bucket`.

## Synthetic flag exceptions

Every fixture carries `"synthetic": true` at the top level, except the fixtures
listed below (the conformance hygiene check reads this list):

<!-- synthetic-exceptions:start -->
"""
README_TAIL = "<!-- synthetic-exceptions:end -->\n"


def write(root: Path) -> None:
    fixtures = root / "fixtures"
    for child in fixtures.iterdir() if fixtures.exists() else []:
        if child.is_dir() and child.name != "vectors":
            shutil.rmtree(child)
    for rel, doc in sorted(FILES.items()):
        p = fixtures / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [f"- `{rel}`: {why}" for rel, why in sorted(SYNTHETIC_EXCEPTIONS)]
    (fixtures / "README.md").write_text(README_HEAD + "\n".join(lines) + "\n" + README_TAIL, encoding="utf-8")
    manifest = {
        "version": 1,
        "description": "Language-neutral conformance cases: every fixture with its schema, expected outcome, expected error code (invalid) and semantic-check context. Generated by contracts/python/scripts/gen_fixtures.py.",
        "cases": sorted(CASES, key=lambda c: c["fixture"]),
    }
    (root / "conformance").mkdir(exist_ok=True)
    (root / "conformance" / "cases.yaml").write_text(
        "# Generated by contracts/python/scripts/gen_fixtures.py - do not edit by hand.\n" + yaml.safe_dump(manifest, sort_keys=False, width=200),
        encoding="utf-8",
    )


def main() -> None:
    global ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = ap.parse_args()
    ROOT = args.root.resolve()
    build()
    write(ROOT)
    print(f"{len(FILES)} fixtures, {len(SYNTHETIC_EXCEPTIONS)} synthetic exceptions")


ROOT = DEFAULT_ROOT

if __name__ == "__main__":
    main()
