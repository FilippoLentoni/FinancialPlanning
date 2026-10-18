"""Stable, human-friendly S3 views of the beta modeling reports (modeling-report Lambda).

Every report revision is first archived write-once by :func:`reports.write_report` under
``modeling/v1/<experiment>/forward/<period>/<revision>/`` (immutable, checksum addressed). This
module then copies the same bytes to overwritable keys under :data:`VIEW_ROOT`, the one prefix
of the reports bucket the bucket policy exempts from the write-once rule
(``core.artifacts.MUTABLE_PREFIXES``)::

    modeling-views/v1/<experiment>/latest/report.html|csv|json   newest daily report
    modeling-views/v1/<experiment>/latest/manifest.json|head.json
    modeling-views/v1/<experiment>/daily/<NY-date>/...            per America/New_York day
    modeling-views/v1/<experiment>/weekly/<from>_<to>/...         per seven-day window
    modeling-views/v1/<experiment>/weekly/latest/...              newest weekly window
    modeling-views/v1/<experiment>/review/<NY-date>.md|json       end-of-day review

``head.json`` (written last) names the immutable revision a view was copied from. A view is
never regressed: an older period, an older data cutoff or a non-final revision of an already
finalized period leaves it unchanged (the immutable revision is still archived).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from ..core.artifacts import ArtifactNotFound, ArtifactStore
from ..core.errors import PlatformError

__all__ = ["VIEW_ROOT", "VIEW_PROTOCOL", "view_prefix", "eligible", "publish_report_views", "publish_review", "safe_experiment"]

VIEW_ROOT = "modeling-views/v1/"
VIEW_PROTOCOL = "finplan-modeling-view/1"
CONTENT_TYPES = {"html": "text/html; charset=utf-8", "csv": "text/csv; charset=utf-8", "json": "application/json", "md": "text/markdown; charset=utf-8"}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIOD = re.compile(r"^\d{4}-\d{2}-\d{2}(?:_\d{4}-\d{2}-\d{2})?$")


def safe_experiment(experiment: str) -> str:
    """Same key-safe spelling ``reports.write_report`` uses for the immutable archive."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", experiment)


def view_prefix(experiment: str) -> str:
    return f"{VIEW_ROOT}{safe_experiment(experiment)}/"


def _read_head(store: ArtifactStore, key: str) -> dict[str, Any] | None:
    try:
        raw, _ = store.get("reports", key)
    except ArtifactNotFound:
        return None
    except PlatformError:
        return None  # checksum-damaged head: never blocks repair
    try:
        head = json.loads(raw)
    except ValueError:
        return None  # a damaged head never blocks repair
    return head if isinstance(head, dict) and head.get("protocol") == VIEW_PROTOCOL else None


def eligible(new: Mapping[str, Any], old: Mapping[str, Any] | None) -> bool:
    """May ``new`` replace the view currently described by ``old``?"""
    if old is None:
        return True
    new_period, old_period = (new["end_day"], new["start_day"]), (str(old.get("end_day")), str(old.get("start_day")))
    if new_period != old_period:
        return new_period > old_period
    if old.get("finalized") and not new.get("finalized"):
        return False
    return str(new.get("data_cutoff") or "") >= str(old.get("data_cutoff") or "") and str(new["generated_at"]) >= str(old.get("generated_at") or "")


def _put_view(store: ArtifactStore, prefix: str, files: Mapping[str, bytes], head: Mapping[str, Any]) -> list[str]:
    keys = []
    for name, payload in files.items():
        key = prefix + name
        store.put_mutable("reports", key, payload, CONTENT_TYPES.get(name.rsplit(".", 1)[-1], "application/octet-stream"))
        keys.append(key)
    store.put_mutable("reports", prefix + "head.json", json.dumps(head, sort_keys=True, indent=1).encode(), "application/json")
    return keys + [prefix + "head.json"]


def publish_report_views(store: ArtifactStore, *, experiment: str, period: str, files: Mapping[str, bytes], manifest: Mapping[str, Any], finalized: bool, run_mode: str, published_at: str) -> dict[str, Any]:
    """Copy one archived revision (``report.html|csv|json`` + ``manifest.json``) to its views."""
    if not _PERIOD.fullmatch(period):
        raise ValueError(f"invalid report period {period!r}")
    missing = {"report.html", "report.csv", "report.json", "manifest.json"} - set(files)
    if missing:
        raise ValueError(f"archived revision lacks {sorted(missing)}")
    start_day, _, end_day = period.partition("_")
    weekly = bool(end_day)
    end_day = end_day or start_day
    root = view_prefix(experiment)
    head = {"protocol": VIEW_PROTOCOL, "experiment_id": experiment, "period": "weekly" if weekly else "daily", "start_day": start_day, "end_day": end_day,
            "revision": manifest["revision"], "generated_at": manifest["generated_at"], "data_cutoff": manifest.get("data_cutoff"), "finalized": bool(finalized),
            "run_mode": run_mode, "published_at": published_at,
            "immutable_prefix": f"modeling/v1/{safe_experiment(experiment)}/{manifest.get('mode', 'forward')}/{period}/{manifest['revision']}/",
            "checksums": {name: "sha256:" + hashlib.sha256(raw).hexdigest() for name, raw in sorted(files.items())}}
    targets = {"period": root + (f"weekly/{period}/" if weekly else f"daily/{period}/"), "latest": root + ("weekly/latest/" if weekly else "latest/")}
    written: dict[str, list[str]] = {}
    skipped: dict[str, Any] = {}
    for name, prefix in targets.items():
        old = _read_head(store, prefix + "head.json")
        if eligible(head, old):
            written[name] = _put_view(store, prefix, files, head)
        else:
            skipped[name] = {"kept_revision": (old or {}).get("revision"), "prefix": prefix}
    return {"head": head, "written": written, "skipped": skipped, "prefixes": targets}


def publish_review(store: ArtifactStore, *, experiment: str, day: str, review_json: bytes, review_md: bytes) -> dict[str, Any]:
    """Immutable checksum-addressed copy plus the stable ``review/<day>.md|json`` views."""
    if not _DAY.fullmatch(day):
        raise ValueError(f"invalid review day {day!r}")
    digest = hashlib.sha256(review_json + b"\0" + review_md).hexdigest()[:20]
    immutable = f"modeling/v1/{safe_experiment(experiment)}/review/{day}/{digest}/"
    for name, raw in (("review.json", review_json), ("review.md", review_md)):
        store.put_once_or_verify("reports", immutable + name, raw, content_type=CONTENT_TYPES[name.rsplit(".", 1)[-1]])
    root = view_prefix(experiment) + "review/"
    store.put_mutable("reports", root + f"{day}.json", review_json, CONTENT_TYPES["json"])
    store.put_mutable("reports", root + f"{day}.md", review_md, CONTENT_TYPES["md"])
    return {"immutable_prefix": immutable, "views": [root + f"{day}.md", root + f"{day}.json"]}
