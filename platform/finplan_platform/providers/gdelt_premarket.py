"""Versioned four-batch GKG2.1 premarket numeric source; never article text.

Attribution: The GDELT Project, https://www.gdeltproject.org/ .
"""
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import math
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZipFile
from zoneinfo import ZoneInfo
from .gdelt_metadata import ALIASES, canonical, checksum, freeze_rights, normalized, stamp

FEATURE_VERSION = "gdelt-gkg-v2-premarket-organization-tone-v1"
MAX_COMPRESSED = 32 * 1024 * 1024
MAX_UNCOMPRESSED = 256 * 1024 * 1024
NY = ZoneInfo("America/New_York")


def window(day):
    start = datetime.combine(day, time(7, 45), NY)
    return start, start + timedelta(hours=1), datetime.combine(day, time(9), NY)


def aggregate(body, batch_at):
    name = batch_at.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    records = {}
    rows = web_rows = 0
    with ZipFile(io.BytesIO(body)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename != name + ".gkg.csv" or members[0].file_size > MAX_UNCOMPRESSED:
            raise ValueError("unexpected or oversized GKG2 archive")
        with archive.open(members[0]) as stream:
            for raw in stream:
                fields = raw.rstrip(b"\r\n").split(b"\t")
                if len(fields) != 27 or not fields[0].startswith((name + "-").encode()) or fields[1] != name.encode():
                    raise ValueError("GKG2 schema or timestamp mismatch")
                rows += 1
                tone = float(fields[15].split(b",")[0])
                if not math.isfinite(tone) or not -100 <= tone <= 100:
                    raise ValueError("invalid GKG2 tone")
                if fields[2] != b"1":
                    continue
                web_rows += 1
                if not fields[4]:
                    raise ValueError("missing GKG2 web document identifier")
                names = set()
                for token in fields[13].split(b";"):
                    # Exact aliases are ASCII. Unrelated metadata can carry
                    # legacy encodings; no lossy decoding of matched fields.
                    try:
                        names.add(normalized(token.decode("ascii")))
                    except UnicodeDecodeError:
                        continue
                records[hashlib.sha256(fields[4]).hexdigest()] = (names, tone)
    if not rows:
        raise ValueError("empty GKG2 archive")
    return records, rows, web_rows


def freeze_batch(batch_at, directory, *, opener=urlopen, now=None):
    now = now or datetime.now(UTC)
    name = batch_at.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    url = "https://data.gdeltproject.org/gdeltv2/" + name + ".gkg.csv.zip"
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw, meta = folder / (name + ".zip"), folder / (name + ".source.json")
    if meta.exists():
        source = json.loads(meta.read_text())
        body = raw.read_bytes()
        if checksum(body) != source["archive_checksum"]:
            raise ValueError("frozen GKG2 source checksum mismatch")
    else:
        try:
            with opener(Request(url, headers={"User-Agent": "finplan-personal-research/1.0"}), timeout=60) as response:
                if response.geturl() != url:
                    raise ValueError("GKG2 archive origin changed")
                body = response.read(MAX_COMPRESSED + 1)
                if len(body) > MAX_COMPRESSED:
                    raise ValueError("GKG2 archive exceeds bound")
                modified = parsedate_to_datetime(response.headers["Last-Modified"]).astimezone(UTC)
                source = {"batch_at": stamp(batch_at), "url": url,
                          "archive_checksum": checksum(body), "archive_bytes": len(body),
                          "source_last_modified": stamp(modified), "source_etag": response.headers.get("ETag"),
                          "source_available_at": stamp(max(modified, batch_at + timedelta(minutes=15))),
                          "retrieved_at": stamp(now)}
        except HTTPError as exc:
            if exc.code != 404:
                raise
            return None, {}, (0, 0), {"batch_at": stamp(batch_at), "url": url, "http_status": 404,
                                 "retrieved_at": stamp(now), "archive_checksum": None, "error": "archive_not_found_at_retrieval"}
        except (URLError, TimeoutError) as exc:
            return None, {}, (0, 0), {"batch_at": stamp(batch_at), "url": url, "http_status": 0,
                                 "retrieved_at": stamp(now), "archive_checksum": None, "error": type(exc).__name__}
        # Verify numeric fields before retaining a usable source vintage.
        aggregate(body, batch_at)
        with raw.open("xb") as stream:
            stream.write(body)
        with meta.open("xb") as stream:
            stream.write(canonical(source))
    records, rows, web_rows = aggregate(body, batch_at)
    attempt = {"batch_at": stamp(batch_at), "url": url, "http_status": 200,
               "retrieved_at": source["retrieved_at"], "archive_checksum": source["archive_checksum"], "error": None}
    return source, records, (rows, web_rows), attempt


def freeze_day(day: date, directory, *, opener=urlopen, now=None):
    now = now or datetime.now(UTC)
    start, end, decision = window(day)
    if now.tzinfo is None or now < end:
        raise ValueError("premarket window is not completed")
    folder = Path(directory) / day.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = folder / (FEATURE_VERSION + ".json")
    if output.exists():
        document = json.loads(output.read_text())
        for source in document["lineage"]["sources"]:
            name = datetime.fromisoformat(source["batch_at"].replace("Z", "+00:00")).strftime("%Y%m%d%H%M%S")
            if checksum((folder / (name + ".zip")).read_bytes()) != source["archive_checksum"]:
                raise ValueError("frozen GKG2 window source changed")
        return document
    sources, attempts, documents, rows, web_rows = [], [], {}, 0, 0
    for minutes in (0, 15, 30, 45):
        batch = (start + timedelta(minutes=minutes)).astimezone(UTC)
        source, records, count, attempt = freeze_batch(batch, folder, opener=opener, now=now)
        attempts.append(attempt)
        if source:
            sources.append(source)
            documents.update(records)  # Last eligible batch wins exact document ID.
            rows += count[0]
            web_rows += count[1]
    captured = len(sources)
    features = []
    for symbol, aliases in ALIASES.items():
        tones = [tone for names, tone in documents.values() if set(aliases) & names]
        features.append({"instrument_id": symbol, "article_count": len(tones) if captured else None,
                         "cluster_count": len(tones) if captured else None,
                         "tone": sum(tones) / len(tones) if tones else None, "missing": captured < 4})
    rights_path = Path(directory) / "rights-evidence.json"
    rights = json.loads(rights_path.read_text()) if rights_path.exists() else freeze_rights(directory, opener=opener)
    document = {"feature_version": FEATURE_VERSION, "source_day": str(day),
                "window_start": stamp(start), "window_end": stamp(end), "decision_cutoff": stamp(decision),
                "quality_status": "complete" if captured == 4 else "partial" if captured else "unavailable",
                "expected_batches": 4, "captured_batches": captured,
                "missing_batches": [a["batch_at"] for a in attempts if a["http_status"] != 200],
                "row_count": rows, "unique_documents": len(documents), "duplicate_documents": web_rows - len(documents),
                "rejected_rows": 0, "features": features, "aliases": {s: list(v) for s, v in ALIASES.items()},
                "disclosures": ["Exact organization mentions; document tone is not stock-specific sentiment.",
                  "Four quarter-hour GKG2.1 metadata archives; no source article text.",
                  "cluster_count equals unique document identifiers, not semantic financial-event clusters.",
                  "Healthy zero matches have count zero, tone null and full source coverage; absent sources are unknown.",
                  "Partial-window counts are observed lower bounds, not complete totals.",
                  "Missing archives describe today's historical archive audit, not proof of historical feed downtime.",
                  "VOO uses Vanguard organization mentions, not constituent-weighted ETF sentiment."],
                "lineage": {"source": "gdelt-gkg-v2", "sources": sources, "attempts": attempts,
                    "source_available_at": max((s["source_available_at"] for s in sources), default=None),
                    "retrieved_at": stamp(now), "platform_available_at": stamp(now),
                    "availability_basis": "max(provider_object_last_modified,batch_at+15min); historical_source_proxy_not_platform_first_seen",
                    "rights": rights, "attribution": "The GDELT Project, https://www.gdeltproject.org/"}}
    with output.open("xb") as stream:
        stream.write(canonical(document))
    return document
