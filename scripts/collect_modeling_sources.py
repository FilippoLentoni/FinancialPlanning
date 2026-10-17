#!/usr/bin/env python3
"""Platform-owned bounded beta quote/news collection before model execution.

Retrieval remains in FinancialPlanning (DS-12). Every result, including explicit
provider failures, is immutable and queryable through the platform evidence path.
FinanceModel receives stored evidence; it never calls market providers indirectly.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
from finplan_platform.core.modeling_evidence import ModelingEvidence, _id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("quotes", "research"), required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--include-experiment", action="append", default=[])
    parser.add_argument("--slot", required=True)
    parser.add_argument("--symbols", help="Bootstrap universe from verified existing policy exports; later registry must match")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    backend = ModelingEvidence.from_ec2(region="us-east-2")
    experiments = [args.experiment, *args.include_experiment]
    if len(experiments) != len(set(experiments)) or len(experiments) > 8:
        parser.error("at most eight distinct bounded experiment cohorts")
    aid = "__valuation_quotes__" if args.kind == "quotes" else "__research_source__"
    kind = "valuation_quotes" if args.kind == "quotes" else "research_source"
    destinations = experiments if args.kind == "quotes" else [args.experiment + "-interim"]
    existing = [backend.repo.get("research_input", _id("mi", experiment, aid, args.slot, kind)) for experiment in destinations]
    if all(existing):
        receipt = {"kind": kind, "slot": args.slot, "status": "reused_immutable_source", "inputs": [row.doc for row in existing]}
    else:
        # If another cohort's capture succeeded before a crash, reuse exactly those
        # original source bytes instead of collecting a replacement quote/news set.
        original = next((row for row in existing if row is not None), None)
        if original:
            payload = backend.read_input(original.id)["payload"]
        elif args.kind == "quotes":
            from finplan_platform.providers.current_quotes import collect_quotes
            symbols = set(filter(None, (args.symbols or "").split(",")))
            for experiment in experiments:
                registry = backend.read_registry(experiment)
                if registry:
                    symbols.update(registry.get("comparison_policy", {}).get("investable_universe", []))
            if not symbols:
                parser.error("first collection requires verified bootstrap symbols or a persisted registry")
            try:
                payload = collect_quotes(symbols)
            except Exception as exc:
                failed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                payload = {"status": "failed", "acquisition_failure": type(exc).__name__ + ": " + str(exc),
                           "quotes": [], "source_artifacts": [], "available_at": failed_at,
                           "coverage": [{"symbol": symbol, "status": "failed", "error_code": type(exc).__name__}
                                        for symbol in sorted(symbols)]}
        else:
            from finplan_platform.providers.financial_research import collect
            try:
                payload = collect(args.output / "collection")
            except Exception as exc:
                failed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                payload = {"status": "failed", "acquisition_failure": type(exc).__name__ + ": " + str(exc),
                           "news": [], "universe": None, "coverage": [], "available_at": failed_at,
                           "workers": [{"worker_id": "universe_acquisition", "status": "failed",
                                        "error_code": type(exc).__name__, "finished_at": failed_at}],
                           "limitations": ["No research policy may treat failed collection as neutral news"]}
        at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        payload = {**payload, "availability_cutoff": payload.get("available_at", at), "collection_owner": "financialplanning",
                   "evidence_purpose": "valuation_only" if args.kind == "quotes" else "forward_financial_research",
                   "scheduled_slot": args.slot, "environment": "beta"}
        refs = [row.doc if row is not None else backend.capture_input(experiment, aid, args.slot, payload, None, at, kind=kind)
                for experiment, row in zip(destinations, existing)]
        for ref in refs:
            actual = backend.read_input(ref["input_id"])
            if actual["metadata"]["checksum"] != ref["checksum"] or actual["payload"] != payload:
                raise RuntimeError("persisted source readback mismatch")
        receipt = {"kind": kind, "slot": args.slot, "status": "captured", "inputs": refs}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / (args.kind + "-source-receipt.json")).write_text(json.dumps(receipt, sort_keys=True))
    print(json.dumps({"kind": receipt["kind"], "slot": args.slot, "status": receipt["status"],
                      "input_ids": [ref["input_id"] for ref in receipt["inputs"]]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
