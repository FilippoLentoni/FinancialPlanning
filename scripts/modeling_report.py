#!/usr/bin/env python3
"""Render actual persisted beta evidence; never call a provider/model or demo builder."""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
from finplan_platform.reporting.reports import EvidenceError, aggregate, aggregate_interim, consolidate, write_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--include-interim", action=argparse.BooleanOptionalAction, default=True, help="Include actual primary-experiment30-minute interim recommendations (default enabled); exclude from primary ranking")
    parser.add_argument("--include-experiment", action="append", default=[], help="Include another isolated cohort in this same consolidated report (repeatable)")
    parser.add_argument("--day", help="Reporting day in America/New_York (defaults to today)")
    parser.add_argument("--weekly", action="store_true", help="Rolling seven calendar days ending on --day")
    parser.add_argument("--finalized", action="store_true", help="Archive closing revision only after 17:00 America/New_York")
    parser.add_argument("--output", type=Path, default=Path("/home/ec2-user/projects/reports/beta-modeling"))
    parser.add_argument("--mode", choices=("forward", "controlled_replay"), default="forward")
    parser.add_argument("--no-s3-archive", action="store_true", help="Only for diagnosis: still query actual Dynamo/S3 evidence")
    args = parser.parse_args(argv)
    from finplan_platform.core.modeling_evidence import ModelingEvidence
    evidence = ModelingEvidence.from_ec2(region="us-east-2")
    now = datetime.now(timezone.utc)
    day = args.day or now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    end = datetime.fromisoformat(day).date()
    close = datetime.fromisoformat(day + "T17:00:00").replace(tzinfo=ZoneInfo("America/New_York"))
    if args.finalized and now < close and args.mode == "forward":
        raise EvidenceError("A forward day cannot be finalized before its 17:00 reporting cutoff")
    reports = []
    for experiment in [args.experiment, *args.include_experiment]:
        registry = evidence.read_registry(experiment)
        if not registry:
            raise EvidenceError(f"No persisted approach registry exists for {experiment}")
        report = aggregate(evidence, experiment_id=experiment, registry=registry["approaches"],
                           start_day=(end-timedelta(days=6)).isoformat() if args.weekly else day,
                           end_day=day, generated_at=now.isoformat(), finalized=args.finalized, mode=args.mode,
                           comparison_policy=registry.get("comparison_policy", {}))
        reports.append(report)
    if args.include_interim:
        interim = aggregate_interim(evidence, primary_experiment=args.experiment, primary_registry=evidence.read_registry(args.experiment),
            start_day=(end-timedelta(days=6)).isoformat() if args.weekly else day, end_day=day,
            generated_at=now.isoformat(), finalized=args.finalized, mode=args.mode)
        if interim:
            reports.append(interim)
    report = consolidate(reports)
    result = write_report(report, args.output, artifact_store=None if args.no_s3_archive else evidence.store)
    print(json.dumps({"directory": result["directory"], "archive": result["archive"], "revision": result["revision"],
                      "runs": len(report["runs"]), "evidence_failures": report["evidence_failures"],
                      "missing_slots": report["missing_slots"], "unranked": [{"approach_id": b["approach_id"], "reasons": b["ranking_blockers"]} for b in report["leaderboard"] if b["rank"] is None]}, indent=2))
    return 2 if report["evidence_failures"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidenceError as error:
        print(f"Report evidence failure: {error}", file=sys.stderr)
        raise SystemExit(2)
