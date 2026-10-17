"""Invoke the authorized producer for bounded historical numeric-source capture.

The operator never writes authoritative buckets. Each producer invocation checks
the project budget, retrieves the approved fixed origin, and verifies the local
archive checksum when one exists. No automatic retry of a failed invocation.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--local-corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    if not 0 <= (args.end - args.start).days <= 365 or args.end >= datetime.now(UTC).date():
        parser.error("capture requires 1..366 prior UTC archive days")
    import boto3
    from botocore.config import Config
    client = boto3.Session(region_name="us-east-2").client("lambda", config=Config(read_timeout=330, retries={"total_max_attempts": 1}))
    days = [args.start + timedelta(days=i) for i in range((args.end-args.start).days+1)]
    rows, failures = [], []
    def invoke(day):
        event = {"source": "finplan.gdelt-metadata", "source_day": str(day)}
        frozen = args.local_corpus / day.strftime("%Y/%m/%d/source.json")
        if frozen.exists():
            event["expected_archive_checksum"] = json.loads(frozen.read_text())["archive_checksum"]
        result = client.invoke(FunctionName="finplan-beta-financialplanning-ingestion-handler", InvocationType="RequestResponse", Payload=json.dumps(event).encode())
        body = json.loads(result["Payload"].read())
        if result.get("FunctionError") or body.get("status") not in ("captured", "already_frozen"):
            return {"source_day": str(day), "status": "failed", "error_type": body.get("errorType", "unexpected_producer_result")}
        return body
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        pending = {executor.submit(invoke, day): day for day in days}
        for future in as_completed(pending):
            day = pending[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"source_day": str(day), "status": "failed", "error_type": type(exc).__name__}
            (failures if result["status"] == "failed" else rows).append(result)
            print(json.dumps(result), flush=True)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"captured_at": datetime.now(UTC).isoformat(), "start_date": str(args.start), "end_date": str(args.end),
              "expected_days": len(days), "captured_days": len(rows), "complete": not failures,
              "captures": sorted(rows, key=lambda r: r["source_day"]), "failures": sorted(failures, key=lambda r: r["source_day"])}
    path = args.output / ("central-capture-"+datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")+".json")
    with path.open("x") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({"report": str(path), "complete": not failures, "captured_days": len(rows), "expected_days": len(days)}))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
