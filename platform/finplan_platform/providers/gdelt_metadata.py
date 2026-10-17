"""GDELT's daily machine metadata, with frozen source and retrieval vintages.

This is a separate research source, not paid search headlines or article text.
Attribution: The GDELT Project, https://www.gdeltproject.org/ .
Dataset terms: https://gdeltproject.org/about.html (Terms of Use).
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile

FEATURE_VERSION = "gdelt-gkg-v1-organization-tone-v1"
MAX_COMPRESSED = 100 * 1024 * 1024
MAX_UNCOMPRESSED = 512 * 1024 * 1024
HEADER = "DATE NUMARTS COUNTS THEMES LOCATIONS PERSONS ORGANIZATIONS TONE CAMEOEVENTIDS SOURCES SOURCEURLS".split()
# Exact, normalized organization entities only. Ambiguous 'meta', 'apple',
# 'amazon' and ticker substrings are deliberately excluded.
ALIASES = {
    "GOOGL": ("google", "google inc", "google llc", "alphabet inc"),
    "AMZN": ("amazon.com", "amazon.com inc", "amazon inc", "amazon web services", "amazon web services inc"),
    "NVDA": ("nvidia", "nvidia corp", "nvidia corporation"),
    "MSFT": ("microsoft", "microsoft corp", "microsoft corporation"),
    "AAPL": ("apple inc", "apple computer inc"),
    "META": ("facebook", "facebook inc", "meta platforms", "meta platforms inc"),
    "TSLA": ("tesla inc", "tesla motors", "tesla motors inc"),
    "VOO": ("vanguard group", "vanguard group inc", "vanguard s&p 500 etf"),
    "NFLX": ("netflix", "netflix inc"),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def checksum(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@lru_cache(maxsize=65536)
def normalized(value):
    return re.sub(r"\s+", " ", value.strip().lower().rstrip("."))


def freeze_rights(directory: str | Path, *, opener=urlopen):
    """Retain the provider's explicit metadata-use terms and attribution duty."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path, manifest = root / "rights-evidence.html", root / "rights-evidence.json"
    if manifest.exists():
        document = json.loads(manifest.read_text())
        if checksum(path.read_bytes()) != document["evidence_checksum"]:
            raise ValueError("GDELT rights evidence checksum mismatch")
        return document
    url = "https://gdeltproject.org/about.html"
    with opener(Request(url), timeout=30) as response:
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("GDELT rights evidence exceeds bound")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body.decode())).lower()
    if "unlimited and unrestricted use" not in text or "include a citation" not in text:
        raise ValueError("GDELT metadata rights terms changed; review required")
    document = {"evidence_url": url, "evidence_checksum": checksum(body),
                "retrieved_at": stamp(datetime.now(UTC)),
                "rights_status": "research_and_training", "scope": "released_GDELT_machine_metadata_only",
                "attribution": "The GDELT Project, https://www.gdeltproject.org/"}
    with path.open("xb") as stream:
        stream.write(body)
    with manifest.open("xb") as stream:
        stream.write(canonical(document))
    return document


def aggregate(body: bytes, day: date, symbols=tuple(ALIASES)):
    """Aggregate exact GKG organization clusters, never claim article specificity.

    NUMARTS weights each cluster. The source's document-wide tone is not a
    financial sentiment classifier or a causal assessment of the matched firm.
    """
    if not symbols or len(set(symbols)) != len(symbols) or set(symbols) - ALIASES.keys():
        raise ValueError("unsupported or duplicated GDELT symbols")
    stats = {s: {"article_count": 0, "cluster_count": 0, "tone_sum": 0.0} for s in symbols}
    aliases = {s: set(map(normalized, ALIASES[s])) for s in symbols}
    by_name = {}
    for symbol, names in aliases.items():
        for name in names:
            by_name.setdefault(name, set()).add(symbol)
    rows = rejected = 0
    with ZipFile(io.BytesIO(body)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].file_size > MAX_UNCOMPRESSED or members[0].filename != day.strftime("%Y%m%d.gkg.csv"):
            raise ValueError("unexpected or oversized GDELT archive")
        with archive.open(members[0]) as stream:
            header = stream.readline().decode("utf-8").strip().split("\t")
            if header != HEADER:
                raise ValueError("GDELT daily schema changed")
            for line in stream:
                if not line.strip():
                    continue
                fields = line.decode("utf-8", errors="strict").rstrip("\r\n").split("\t")
                rows += 1
                try:
                    if len(fields) != len(HEADER) or fields[0] != day.strftime("%Y%m%d"):
                        raise ValueError("schema or batch date mismatch")
                    weight = int(fields[1])
                    tone = float(fields[7].split(",")[0])
                    if weight < 1 or not math.isfinite(tone) or not -100 <= tone <= 100:
                        raise ValueError("invalid count or tone")
                except (ValueError, IndexError):
                    rejected += 1
                    continue
                names = {normalized(v) for v in fields[6].split(";") if v.strip()}
                matched = {s for name in names for s in by_name.get(name, ())}
                for symbol in matched:
                    stats[symbol]["article_count"] += weight
                    stats[symbol]["cluster_count"] += 1
                    stats[symbol]["tone_sum"] += weight * tone
    if not rows or rejected:
        raise ValueError(f"GDELT archive quality failed: {rows} rows, {rejected} rejected")
    features = []
    for symbol, value in stats.items():
        count = value["article_count"]
        features.append({"instrument_id": symbol, "article_count": count,
                         "cluster_count": value["cluster_count"],
                         "tone": value["tone_sum"] / count if count else None,
                         "missing": not bool(count)})
    return {"feature_version": FEATURE_VERSION, "source_day": day.isoformat(),
            "row_count": rows, "rejected_rows": rejected, "features": features,
            "aliases": {s: list(ALIASES[s]) for s in symbols},
            "disclosures": ["Exact organization matches may miss aliases or link ambiguous entities.",
                            "Tone describes the whole document, not company financial sentiment.",
                            "VOO uses Vanguard organization mentions, not constituent-weighted ETF news.",
                            "A zero match count differs from an absent or failed archive."]}


def freeze_day(day: date, directory: str | Path, *, symbols=tuple(ALIASES), opener=urlopen, now=None):
    """Retain original ZIP, metadata and derived aggregates without overwriting.

    source_available_at is an explicitly conservative archive-availability proxy;
    platform_available_at is today's actual receipt and is never backdated.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None or day >= now.date():
        raise ValueError("only prior UTC days may be backfilled")
    root = Path(directory) / day.strftime("%Y/%m/%d")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = root / "archive.zip"
    metadata = root / "source.json"
    output = root / (FEATURE_VERSION + ".json")
    parsed = None
    if metadata.exists():
        source = json.loads(metadata.read_text())
        body = raw.read_bytes()
        if checksum(body) != source["archive_checksum"]:
            raise ValueError("frozen GDELT archive checksum mismatch")
    else:
        url = "https://data.gdeltproject.org/gkg/" + day.strftime("%Y%m%d.gkg.csv.zip")
        with opener(Request(url, headers={"User-Agent": "finplan-personal-research/1.0"}), timeout=90) as response:
            if response.geturl() != url:
                raise ValueError("GDELT redirect changed archive origin")
            modified = parsedate_to_datetime(response.headers["Last-Modified"]).astimezone(UTC)
            body = response.read(MAX_COMPRESSED + 1)
            if len(body) > MAX_COMPRESSED:
                raise ValueError("GDELT archive exceeds compressed bound")
            # Date-only GKG batches are not available during their own day. Use
            # next-day noon UTC plus actual source modification when later.
            available = max(modified, datetime.combine(day + timedelta(days=1), time(12), UTC))
            retrieved = datetime.now(UTC)
            source = {"source": "gdelt-gkg-v1", "url": url, "source_day": day.isoformat(),
                      "archive_checksum": checksum(body), "archive_bytes": len(body),
                      "source_last_modified": stamp(modified), "source_etag": response.headers.get("ETag"),
                      "source_available_at": stamp(available), "retrieved_at": stamp(retrieved),
                      "platform_available_at": stamp(retrieved),
                      "availability_basis": "max(provider_object_last_modified, next_day_12UTC); historical_source_proxy_not_platform_first_seen",
                      "rights_evidence_url": "https://gdeltproject.org/about.html",
                      "rights_scope": "GDELT released machine metadata; no source article text",
                      "attribution": "The GDELT Project, https://www.gdeltproject.org/"}
            rights_path = Path(directory) / "rights-evidence.json"
            if rights_path.exists():
                rights = json.loads(rights_path.read_text())
                if checksum((Path(directory) / "rights-evidence.html").read_bytes()) != rights["evidence_checksum"]:
                    raise ValueError("GDELT rights evidence changed")
                source["rights"] = rights
        # Verify before declaring this archive a usable source. Failed data is
        # retained separately by the bounded caller's error report.
        parsed = aggregate(body, day, symbols)
        if raw.exists():
            if checksum(raw.read_bytes()) != source["archive_checksum"]:
                raise ValueError("existing GDELT source differs; review source revision")
        else:
            with raw.open("xb") as stream:
                stream.write(body)
        with metadata.open("xb") as stream:
            stream.write(canonical(source))
    document = {**(parsed or aggregate(body, day, symbols)), "lineage": source}
    encoded = canonical(document)
    if output.exists():
        if output.read_bytes() != encoded:
            raise ValueError("GDELT feature definition changed without a new version")
    else:
        with output.open("xb") as stream:
            stream.write(encoded)
    return {"source_day": day.isoformat(), "feature_path": str(output),
            "feature_checksum": checksum(encoded), "archive_checksum": source["archive_checksum"],
            "archive_bytes": source["archive_bytes"], "source_available_at": source["source_available_at"],
            "row_count": document["row_count"], "features": document["features"]}
