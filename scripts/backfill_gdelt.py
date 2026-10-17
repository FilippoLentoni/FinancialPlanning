"""Bounded private GDELT machine-metadata archive capture, with honest coverage.

Attribution: The GDELT Project, https://www.gdeltproject.org/ .
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
from finplan_platform.providers.gdelt_metadata import ALIASES, FEATURE_VERSION, freeze_day, freeze_rights


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    if not 0 <= (args.end - args.start).days <= 365 or args.end >= datetime.now(UTC).date():
        parser.error("capture requires 1..366 prior UTC archive days")
    days = [args.start + timedelta(days=i) for i in range((args.end - args.start).days + 1)]
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    freeze_rights(args.output)
    rows, failures = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(freeze_day, day, args.output): day for day in days}
        for future in as_completed(futures):
            day = futures[future]
            try:
                row = future.result()
                rows.append(row)
                print(json.dumps({"source_day": str(day), "status": "frozen", "rows": row["row_count"]}), flush=True)
            except Exception as exc:
                failures.append({"source_day": str(day), "error_type": type(exc).__name__, "detail": str(exc)[:250]})
                print(json.dumps({"source_day": str(day), "status": "failed", "error_type": type(exc).__name__}), flush=True)
    report = {"feature_version": FEATURE_VERSION, "started_source_day": str(args.start), "ended_source_day": str(args.end),
              "captured_at": datetime.now(UTC).isoformat(), "expected_archive_days": len(days), "captured_archive_days": len(rows),
              "coverage_complete": not failures and len(rows) == len(days), "failures": sorted(failures, key=lambda r: r["source_day"]),
              "archive_bytes": sum(r["archive_bytes"] for r in rows), "symbols": list(ALIASES),
              "archives": sorted(rows, key=lambda r: r["source_day"]),
              "training_status": "distinct_feature_adapter_required; capture_is_not_model_training",
              "attribution": "The GDELT Project, https://www.gdeltproject.org/"}
    path = args.output / ("capture-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    with path.open("x") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != "archives"} | {"report": str(path)}))
    return 0 if report["coverage_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
