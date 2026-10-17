"""Capture the previous UTC day's GDELT metadata before the price pipeline.

Attribution: The GDELT Project, https://www.gdeltproject.org/ .
"""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
from finplan_platform.providers.gdelt_metadata import freeze_day, freeze_rights


def collect(now=None, directory=None):
    now = now or datetime.now(UTC)
    session = now.astimezone(ZoneInfo("America/New_York")).date()
    root = Path(directory or ROOT.parent / ".worktmp/gdelt-daily-v1")
    freeze_rights(root)
    row = freeze_day(now.date() - timedelta(days=1), root)
    available = datetime.fromisoformat(row["source_available_at"].replace("Z", "+00:00"))
    return {"status": "captured" if available <= now else "source_not_yet_available_at_cutoff",
            "decision_session": str(session), "captured_at": datetime.now(UTC).isoformat(),
            "archive": row, "model_consumption_verified": False}


if __name__ == "__main__":
    result = collect()
    directory = ROOT.parent / ".worktmp/gdelt-daily-v1/daily-status" / result["decision_session"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    with path.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result | {"readiness_report": str(path)}))
