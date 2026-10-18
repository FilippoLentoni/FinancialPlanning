"""Beta modeling competition Lambdas (infra/stacks/modeling.py): ``modeling-sources`` and ``modeling-report``.

Both run the existing beta modeling CLIs in process (the bundle carries them as the ``scripts``
package; ``scripts/lambda_bundle.py`` ``MODELING_SCRIPTS``) instead of re-implementing them, with
a fresh ``/tmp`` working directory per invocation. Nothing is written outside S3 (reports bucket)
and DynamoDB (modeling evidence tables).

``sources_handler`` (``finplan-<env>-financialplanning-modeling-sources``)::

    {"kind": "quotes"|"research", "slot": "2026-10-19T13:45:00Z", "experiment"?: str, "symbols"?: [str] | "A,B"}
      -> {"status": "captured"|"reused_immutable_source", "kind", "slot", "experiment", "input_ids": [...]}
    {"action": "preflight"} -> {"status": "ok", ...}      (reads only)

``report_handler`` (``finplan-<env>-financialplanning-modeling-report``)::

    {"mode": "current"|"eod"|"weekly"|"closed_week", "experiment"?: str, "day"?: "YYYY-MM-DD",
     "slot"?|"scheduled_time"?: ISO instant}
      -> {"status": "ok"|"evidence_failures", "mode", "experiment", "day", "revision", "views": {...}, "review"?: {...}}
    {"mode": "preflight"} or {"action": "preflight"} -> {"status": "ok", ...}   (reads only)

``day`` defaults to the America/New_York date of ``slot``/``scheduled_time`` (the scheduler's
``<aws.scheduler.scheduled-time>``), else of now. Modes map to the CLI flags the dev-box timers used:
``current`` = ``--day D``; ``eod`` = ``--day D --finalized`` plus the day review; ``weekly`` =
``--day D --weekly``; ``closed_week`` = ``--day D --weekly --closed-week``; always ``--no-include-interim``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

__all__ = ["ModelingEventError", "sources_handler", "report_handler", "normalize_slot", "report_day", "report_argv"]

EASTERN = ZoneInfo("America/New_York")
KINDS = ("quotes", "research")
REPORT_MODES = ("current", "eod", "weekly", "closed_week")
REGION = "us-east-2"


class ModelingEventError(ValueError):
    """The event is not a valid request (not retryable)."""


def _settings() -> Mapping[str, Any]:
    from ..core.config import load_config

    env = os.environ.get("FINPLAN_ENV", "beta")
    if env != "beta":
        raise ModelingEventError(f"the modeling competition pipeline is beta only (FINPLAN_ENV={env})")
    mc = load_config(env).modeling_competition
    if not mc:
        raise ModelingEventError("config/beta.json has no modeling_competition block")
    return mc


def normalize_slot(value: Any) -> str:
    """UTC ``YYYY-MM-DDTHH:MM:00Z`` from a timezone-qualified ISO instant (whole minutes only)."""
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ModelingEventError(f"slot {value!r} is not an ISO 8601 instant") from None
    if at.utcoffset() is None:
        raise ModelingEventError("slot must be timezone-qualified")
    if at.second or at.microsecond:
        raise ModelingEventError("slot must be a whole minute")
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00Z")


def _experiment(event: Mapping[str, Any], mc: Mapping[str, Any]) -> str:
    experiment = str(event.get("experiment") or mc["experiment_id"])
    if not experiment.replace("-", "").replace("_", "").isalnum() or len(experiment) > 64:
        raise ModelingEventError(f"invalid experiment {experiment!r}")
    return experiment


def _last_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    for start in [0] + [i + 1 for i, c in enumerate(text) if c == "\n" and text[i + 1 : i + 2] == "{"]:
        try:
            value = json.loads(text[start:])
            return value if isinstance(value, dict) else None
        except ValueError:
            continue
    return None


def _run_cli(fn: Any, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(argv)
        except SystemExit as exc:  # argparse usage errors
            rc = exc.code if isinstance(exc.code, int) else 2
    return int(rc or 0), out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _workdir(prefix: str):
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=os.environ.get("FINPLAN_MODELING_TMP") or None))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _evidence():
    from ..core.modeling_evidence import ModelingEvidence

    return ModelingEvidence.from_ec2(region=REGION)


def _preflight(function: str, mc: Mapping[str, Any], experiment: str, *, list_views: bool) -> dict[str, Any]:
    import importlib

    for name in ("scripts.collect_modeling_sources", "scripts.modeling_report", "scripts.modeling_day_review",
                 "finplan_platform.providers.current_quotes", "finplan_platform.providers.financial_research", "finplan_platform.reporting.views"):
        importlib.import_module(name)
    evidence = _evidence()
    registry = evidence.read_registry(experiment)
    out: dict[str, Any] = {"status": "ok", "function": function, "action": "preflight", "experiment": experiment,
                           "registry_present": registry is not None, "symbols": list(mc["symbols"])}
    if list_views:
        from ..reporting.views import view_prefix

        resp = evidence.store._s3.list_objects_v2(Bucket=evidence.store.bucket("reports"), Prefix=view_prefix(experiment), MaxKeys=1)
        out["views_present"] = bool(resp.get("KeyCount"))
    return out


# ===================================================================== modeling-sources
def sources_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    mc = _settings()
    event = dict(event or {})
    experiment = _experiment(event, mc)
    if event.get("action") == "preflight":
        return _preflight("modeling-sources", mc, experiment, list_views=False)
    kind = event.get("kind")
    if kind not in KINDS:
        raise ModelingEventError(f"kind must be one of {KINDS}, got {kind!r}")
    slot = normalize_slot(event.get("slot"))
    symbols = event.get("symbols") or mc["symbols"]
    if isinstance(symbols, str):
        symbols = [s for s in symbols.split(",") if s]
    if not isinstance(symbols, list) or not all(isinstance(s, str) and s.replace(".", "").isalnum() for s in symbols):
        raise ModelingEventError("symbols must be a list of tickers")
    from scripts import collect_modeling_sources as cli

    with _workdir("modeling-sources-") as work:
        argv = ["--kind", kind, "--experiment", experiment, "--slot", slot, "--symbols", ",".join(symbols), "--output", str(work)]
        rc, out, err = _run_cli(cli.main, argv)
        printed = _last_json(out)
        if rc != 0 or printed is None:
            raise RuntimeError(f"collect_modeling_sources failed (exit {rc}): {(err or out)[-1500:]}")
    return {"status": printed["status"], "kind": kind, "slot": slot, "experiment": experiment, "input_ids": printed.get("input_ids", [])}


# ===================================================================== modeling-report
def report_day(event: Mapping[str, Any], now: datetime | None = None) -> str:
    day = event.get("day")
    if day:
        try:
            return datetime.strptime(str(day), "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise ModelingEventError(f"day {day!r} is not YYYY-MM-DD") from None
    instant = event.get("slot") or event.get("scheduled_time")
    if instant:
        at = datetime.fromisoformat(normalize_slot(instant).replace("Z", "+00:00"))
    else:
        at = now or datetime.now(UTC)
    return at.astimezone(EASTERN).date().isoformat()


def report_argv(mode: str, experiment: str, day: str, output: Path) -> list[str]:
    base = ["--experiment", experiment, "--no-include-interim", "--output", str(output), "--day", day]
    return base + {"current": [], "eod": ["--finalized"], "weekly": ["--weekly"], "closed_week": ["--weekly", "--closed-week"]}[mode]


def _archived_files(archive: Path) -> dict[str, bytes]:
    return {name: (archive / name).read_bytes() for name in ("report.html", "report.csv", "report.json", "manifest.json")}


def report_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    mc = _settings()
    event = dict(event or {})
    experiment = _experiment(event, mc)
    mode = event.get("mode")
    if mode == "preflight" or event.get("action") == "preflight":
        return _preflight("modeling-report", mc, experiment, list_views=True)
    if mode not in REPORT_MODES:
        raise ModelingEventError(f"mode must be one of {REPORT_MODES}, got {mode!r}")
    day = report_day(event)
    from scripts import modeling_report as cli

    from ..reporting.views import publish_report_views, publish_review

    evidence = _evidence()
    published_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with _workdir("modeling-report-") as work:
        rc, out, err = _run_cli(cli.cli_exit_code, report_argv(mode, experiment, day, work / "out"))
        printed = _last_json(out)
        if rc not in (0, 2) or printed is None or not printed.get("archive"):
            raise RuntimeError(f"modeling_report {mode} failed (exit {rc}): {(err or out)[-1500:]}")
        archive = Path(printed["archive"])
        files = _archived_files(archive)
        manifest = json.loads(files["manifest.json"])
        views = publish_report_views(evidence.store, experiment=experiment, period=archive.parent.name, files=files, manifest=manifest,
                                     finalized=mode in ("eod", "closed_week"), run_mode=mode, published_at=published_at)
    result: dict[str, Any] = {"status": "ok" if rc == 0 else "evidence_failures", "mode": mode, "experiment": experiment, "day": day,
                              "revision": printed.get("revision"), "runs": printed.get("runs"), "evidence_failures": len(printed.get("evidence_failures") or []),
                              "missing_slots": len(printed.get("missing_slots") or []), "views": {"head": views["head"], "written": sorted(views["written"]), "skipped": views["skipped"], "prefixes": views["prefixes"]}}
    if mode == "eod":
        from scripts import modeling_day_review as review_cli

        review = review_cli.review(evidence, experiment, day)
        result["review"] = publish_review(evidence.store, experiment=experiment, day=day,
                                          review_json=json.dumps(review, indent=1, default=str).encode(), review_md=review_cli.markdown(review).encode())
        result["review"]["ranking"] = review["ranking"]
    return result
