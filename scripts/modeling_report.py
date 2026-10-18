#!/usr/bin/env python3
"""Render actual persisted beta evidence; never call a provider/model or demo builder."""
from __future__ import annotations
import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
from finplan_platform.reporting.reports import EvidenceError, PublicationDeferred, aggregate, aggregate_interim, consolidate, write_report
from finplan_platform.reporting import refresh


def report_end_day(day, now, *, closed_week=False):
    end = date.fromisoformat(day) if day else now.astimezone(ZoneInfo("America/New_York")).date()
    return (end - timedelta(days=1) if closed_week else end).isoformat()


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--include-interim", action=argparse.BooleanOptionalAction, default=True, help="Include actual primary-experiment30-minute interim recommendations (default enabled); exclude from primary ranking")
    parser.add_argument("--include-experiment", action="append", default=[], help="Include another isolated cohort in this same consolidated report (repeatable)")
    parser.add_argument("--day", help="Reporting day in America/New_York (defaults to today)")
    parser.add_argument("--repair-weekly-from-queue", action="store_true", help="Repair at most one queued historical weekly window without rebuilding ordinary daily/latest reports")
    parser.add_argument("--weekly", action="store_true", help="Rolling seven calendar days ending on --day")
    parser.add_argument("--closed-week", action="store_true", help="With --weekly, use the seven closed calendar days ending before --day; actual coverage still determines completeness")
    parser.add_argument("--finalized", action="store_true", help="Archive closing revision only after 17:00 America/New_York")
    parser.add_argument("--output", type=Path, default=Path("/home/ec2-user/projects/reports/beta-modeling"))
    parser.add_argument("--mode", choices=("forward", "controlled_replay"), default="forward")
    parser.add_argument("--no-s3-archive", action="store_true", help="Only for diagnosis: still query actual Dynamo/S3 evidence")
    args = parser.parse_args(argv)
    if args.repair_weekly_from_queue and (args.weekly or args.closed_week or args.finalized):
        parser.error("--repair-weekly-from-queue is a separate historical repair invocation")
    if args.closed_week and not args.weekly:
        parser.error("--closed-week requires --weekly")
    # A disabled status-only repair must not create a production output directory.
    if args.repair_weekly_from_queue and not os.environ.get("FINPLAN_REPORT_REFRESH_DIR"):
        print(json.dumps({"weekly_repair":"disabled_no_queue"}));return 0
    from finplan_platform.reporting.render_lock import render_lock
    with render_lock(args.output):
        return _run(args)


def _run(args):
    queue=os.environ.get("FINPLAN_REPORT_REFRESH_DIR")
    repair_tasks=None;repair_state=None
    if args.repair_weekly_from_queue:
        if not queue:
            print(json.dumps({"weekly_repair":"disabled_no_queue"}));return 0
        repair_tasks,repair_state=refresh.read_tasks(Path(queue)/"weekly",[args.experiment,*args.include_experiment],weekly=True)
        if not repair_tasks:
            print(json.dumps({"weekly_repair":"no_pending_tasks","queue":repair_state}));return 0
    from finplan_platform.core.modeling_evidence import ModelingEvidence
    evidence = ModelingEvidence.from_ec2(region="us-east-2")
    now = datetime.now(timezone.utc)
    day = report_end_day(args.day, now, closed_week=args.closed_week)
    args.finalized = args.finalized or args.closed_week
    end = datetime.fromisoformat(day).date()
    close = datetime.fromisoformat(day + "T17:00:00").replace(tzinfo=ZoneInfo("America/New_York"))
    if args.finalized and now < close and args.mode == "forward":
        raise EvidenceError("A forward day cannot be finalized before its 17:00 reporting cutoff")
    experiments = [args.experiment, *args.include_experiment]
    registries = {}
    for experiment in experiments:
        registries[experiment] = evidence.read_registry(experiment)
        if not registries[experiment]:
            raise EvidenceError(f"No persisted approach registry exists for {experiment}")

    def render(start_day, end_day, *, weekly=False, finalized=False, receipts=(), update_latest=True, tasks=()):
        reports = []
        for experiment in experiments:
            registry = registries[experiment]
            report = aggregate(evidence, experiment_id=experiment, registry=registry["approaches"],
                start_day=start_day,end_day=end_day,generated_at=now.isoformat(),finalized=finalized,mode=args.mode,
                comparison_policy=registry.get("comparison_policy", {}),
                required_observations=[r for r in receipts if r["experiment_id"]==experiment])
            reports.append(report)
        if args.include_interim:
            interim=aggregate_interim(evidence,primary_experiment=args.experiment,primary_registry=registries[args.experiment],
                start_day=start_day,end_day=end_day,generated_at=now.isoformat(),finalized=finalized,mode=args.mode)
            if interim:reports.append(interim)
            del interim
        report=consolidate(reports)
        del reports  # The consolidated read model owns every required row/reference.
        if tasks:refresh.ensure_in_report(report,tasks)
        written=write_report(report,args.output,artifact_store=None if args.no_s3_archive else evidence.store,update_latest=update_latest,consume_read_model=True)
        if tasks:
            period_name=report['end_day'] if report['period']=='daily' else report['start_day']+'_'+report['end_day']
            if period_name not in written.get('aliases_published',[]):
                raise PublicationDeferred('Required managed-cost dated alias was not published; immutable repair archive saved and refresh tasks retained for retry')
        return report,written

    if args.repair_weekly_from_queue:
        tasks,queue_state=repair_tasks,repair_state
        first=tasks[0]["task"];window=(first["window_start_day"],first["window_end_day"])
        tasks=[t for t in tasks if (t["task"]["window_start_day"],t["task"]["window_end_day"])==window]
        receipts=refresh.verify_receipts(evidence,tasks)
        closed=datetime.fromisoformat(window[1]+"T17:00:00").replace(tzinfo=ZoneInfo("America/New_York"))<=now
        report,result=render(*window,weekly=True,finalized=closed,receipts=receipts,update_latest=False,tasks=tasks)
        refresh.acknowledge(tasks)
        print(json.dumps({"weekly_repair":"archived","window":window,"revision":result["revision"],"tasks_acknowledged":len(tasks),"queue":queue_state}))
        return 2 if report["evidence_failures"]else 0

    current_tasks=[];current_receipts=[];queue_state=None
    if queue and not args.weekly:
        tasks,queue_state=refresh.read_tasks(queue,experiments)
        historical_days=sorted({t["task"]["reporting_day"] for t in tasks if t["task"]["reporting_day"]!=day})[:2]
        for historical_day in historical_days:
            selected=[t for t in tasks if t["task"]["reporting_day"]==historical_day]
            receipts=refresh.verify_receipts(evidence,selected)
            close_at=datetime.fromisoformat(historical_day+"T17:00:00").replace(tzinfo=ZoneInfo("America/New_York"))
            historical,written=render(historical_day,historical_day,finalized=now>=close_at,receipts=receipts,update_latest=False,tasks=selected)
            refresh.enqueue_existing_weekly(selected,queue_dir=queue,output_dir=args.output,daily_revision=written["revision"],mode=args.mode)
            refresh.acknowledge(selected)
        current_tasks=[t for t in tasks if t["task"]["reporting_day"]==day]
        current_receipts=refresh.verify_receipts(evidence,current_tasks)
    report,result=render((end-timedelta(days=6)).isoformat() if args.weekly else day,day,
        weekly=args.weekly,finalized=args.finalized,receipts=current_receipts,tasks=current_tasks)
    if current_tasks:
        written=result
        refresh.enqueue_existing_weekly(current_tasks,queue_dir=queue,output_dir=args.output,daily_revision=written["revision"],mode=args.mode)
        refresh.acknowledge(current_tasks)
    print(json.dumps({"directory": result["directory"], "archive": result["archive"], "revision": result["revision"],
                      "report_refresh_queue": queue_state, "runs": len(report["runs"]), "evidence_failures": report["evidence_failures"],
                      "missing_slots": report["missing_slots"], "unranked": [{"approach_id": b["approach_id"], "reasons": b["ranking_blockers"]} for b in report["leaderboard"] if b["rank"] is None]}, indent=2))
    return 2 if report["evidence_failures"] else 0


def cli_exit_code(argv=None):
    try:
        return main(argv)
    except PublicationDeferred as error:
        print(f"Report publication deferred: {error}", file=sys.stderr)
        return 75
    except EvidenceError as error:
        print(f"Report evidence failure: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli_exit_code())
