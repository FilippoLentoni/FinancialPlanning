"""Producer-owned immutable GDELT numeric metadata and bounded read API."""
from datetime import UTC, date, datetime, timedelta
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from ..providers.gdelt_metadata import ALIASES, FEATURE_VERSION, canonical, checksum, freeze_day, freeze_rights, stamp
from .context import Caller, OperationContext
from .errors import PlatformError
from .research_news import dt
from .upgrade import current_version

PREFIX = "gdelt-metadata/v1/"


def _require_beta(env):
    if env != "beta":
        raise PlatformError("OPERATION_NOT_PERMITTED", "GDELT numeric research is beta only")


def _audit(ctx, deps, document):
    from .audit import audit_event
    source = document["lineage"]
    record_id = checksum(canonical({"source_day": document["source_day"], "archive_checksum": source["archive_checksum"]}))
    if not deps.repo.audit_events(record_id):
        deps.repo.commit([], audit=[audit_event(ctx, record_id=record_id, record_type="research_metadata",
            operation="capture_gdelt_metadata", new={"source_day": document["source_day"], "feature_version": FEATURE_VERSION},
            feature_checksum=checksum(canonical(document)), archive_checksum=source["archive_checksum"],
            source_available_at=source["source_available_at"], central_available_at=source["central_available_at"])])


def capture(event, *, deps=None, clock=None):
    """Fetch via the producer role; no caller-supplied content or source URL."""
    from .clock import SystemClock
    from .ingestion import default_deps
    deps = deps or default_deps()
    clock = clock or SystemClock()
    _require_beta(deps.config.env)
    now = clock.now()
    expected = event.get("expected_archive_checksum")
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", expected)):
        raise PlatformError.validation("expected_archive_checksum must be a SHA-256 checksum")
    try:
        day = date.fromisoformat(event["source_day"]) if event.get("source_day") else now.date() - timedelta(days=1)
        if day >= now.date():
            raise ValueError("incomplete UTC day")
    except (TypeError, ValueError):
        raise PlatformError.validation("source_day must name a completed prior UTC day") from None
    # News archives publish every calendar day. Weekends still need fresh news
    # for the daily report even though equity bars and paid trials use sessions.
    ctx = OperationContext(caller=Caller(principal=f"finplan-{deps.config.env}-financialplanning-ingestion-handler"),
                           env=deps.config.env, correlation_id="gdelt_" + day.strftime("%Y%m%d"), clock=clock,
                           trigger="on_demand" if event.get("source_day") else "scheduled")
    if deps.budget_gate is not None:
        deps.budget_gate(ctx)
    prefix = PREFIX + day.strftime("%Y/%m/%d/")
    existing = list(deps.store.list_objects("curated", prefix))
    if existing:
        raw, _ = deps.store.get("curated", existing[0][0])
        doc = json.loads(raw)
        if doc.get("source_day") != str(day) or doc.get("feature_version") != FEATURE_VERSION or checksum(raw).split(":")[1] != Path(existing[0][0]).stem:
            raise PlatformError.internal("frozen GDELT capture failed identity verification")
        if expected and doc["lineage"].get("archive_checksum") != expected:
            raise PlatformError.precondition("central GDELT source differs from the requested frozen source vintage")
        _audit(ctx, deps, doc)
        return {"status": "already_frozen", "source_day": str(day), "archives_fetched": 0}
    with TemporaryDirectory(prefix="gdelt-metadata-") as temporary:
        root = Path(temporary)
        rights = freeze_rights(root)
        row = freeze_day(day, root)
        if expected and row["archive_checksum"] != expected:
            raise PlatformError.precondition("GDELT provider archive differs from the requested frozen source vintage")
        document = json.loads(Path(row["feature_path"]).read_text())
        base = root / day.strftime("%Y/%m/%d")
        digest = row["archive_checksum"].split(":")[1]
        deps.store.put_once_or_verify("raw", prefix + digest + ".zip", (base / "archive.zip").read_bytes(), "application/zip")
        rights_digest = rights["evidence_checksum"].split(":")[1]
        deps.store.put_once_or_verify("raw", PREFIX + "rights/" + rights_digest + ".html", (root / "rights-evidence.html").read_bytes(), "text/html")
        document["lineage"]["central_available_at"] = stamp(clock.now())
        body = canonical(document)
        key = prefix + checksum(body).split(":")[1] + ".json"
        artifact = deps.store.put_once_or_verify("curated", key, body)
        _audit(ctx, deps, document)
    return {"status": "captured", "source_day": str(day), "archives_fetched": 1,
            "feature_version": FEATURE_VERSION, "feature_checksum": artifact.checksum,
            "archive_checksum": row["archive_checksum"], "archive_bytes": row["archive_bytes"],
            "source_available_at": document["lineage"]["source_available_at"],
            "central_available_at": document["lineage"]["central_available_at"],
            "row_count": document["row_count"]}


def read(ctx, svc, query, *, prefix=PREFIX, feature_version=FEATURE_VERSION, request_schema="api/gdelt-metadata-request"):
    _require_beta(svc.env)
    from .contract_io import require_valid
    try:
        symbols = query.get("symbols", "GOOGL,AMZN,NVDA,MSFT,AAPL,META,TSLA,VOO").split(",")
        contract_query = {"symbols": symbols, **{k:v for k,v in query.items() if k != "symbols"}}
        require_valid(contract_query, request_schema)
        if not 1 <= len(symbols) <= 9 or len(set(symbols)) != len(symbols) or set(symbols) - ALIASES.keys():
            raise ValueError("symbols")
        start, end = date.fromisoformat(query["start_date"]), date.fromisoformat(query["end_date"])
        if not 0 <= (end - start).days <= 30:
            raise ValueError("31-day bound")
        decision = dt(query["decision_at"]) if query.get("decision_at") else ctx.clock.now()
        if decision > ctx.clock.now():
            raise ValueError("future decision")
        mode = query.get("availability_mode", "platform")
        if mode not in ("platform", "historical_source_proxy"):
            raise ValueError("availability mode")
    except (TypeError, ValueError, KeyError):
        raise PlatformError.validation("GDELT query requires supported symbols, a 1..31 day range and a nonfuture decision cutoff") from None
    rows, missing = [], []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        candidates = []
        for index, (key, _) in enumerate(svc.store.list_objects("curated", prefix + day.strftime("%Y/%m/%d/"))):
            if index >= 8:
                raise PlatformError.precondition("GDELT source exceeds the bounded daily revision window")
            raw, _ = svc.store.get("curated", key)
            document = json.loads(raw)
            if document.get("feature_version") != feature_version or document.get("source_day") != str(day) or checksum(raw).split(":")[1] != Path(key).stem:
                raise PlatformError.internal("GDELT feature identity failed verification")
            lineage = document["lineage"]
            available = lineage["central_available_at"] if mode == "platform" else lineage["source_available_at"]
            if available is None:
                # An explicit missing-window audit has no source availability.
                # Its intended cutoff only positions the unknown-data mask.
                available = document.get("decision_cutoff")
            if available is None or dt(available) > decision:
                continue
            candidates.append({"checksum": checksum(raw), "document": document})
        if candidates:
            # First central capture wins, preserving its source vintage. Later
            # revised archives cannot silently rewrite old decisions.
            candidates.sort(key=lambda item: (item["document"]["lineage"]["central_available_at"], item["checksum"]))
            rows.append(candidates[0])
        else:
            missing.append(str(day))
    return {"contract_version": current_version(), "feature_version": feature_version,
            "symbols": symbols, "decision_at": stamp(decision), "availability_mode": mode,
            "coverage": {"start_date": str(start), "end_date": str(end), "expected_days": (end-start).days+1,
                         "captured_days": len(rows), "complete": not missing, "missing_days": missing},
            "documents": rows}
