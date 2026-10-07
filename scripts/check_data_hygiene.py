#!/usr/bin/env python3
"""Build-stage data-hygiene check (task 6.16; ING-19; design P6a "No market data in the repo").

Two checks, both offline and both required to pass in the build stage:

1. **Fixtures are synthetic.** Every JSON file under the fixture roots (default:
   ``tests/fixtures`` and ``platform/finplan_platform/data``, calendars excluded because they
   hold session schedules, not market data) that looks like market data (has price or row
   fields) must carry a top-level ``"synthetic": true`` and must not match the real-data
   signature list (``scripts/data_hygiene_signatures.json``: raw-provider payload markers and
   hashes of real rows).
2. **No pipeline suite enables the real provider.** No repository file outside the opt-in live
   test and documentation sets ``FINPLAN_LIVE_PROVIDER_TEST`` to a true value, every test
   module that imports the real ``yfinance`` library is marked ``live_provider``, and every
   environment configuration with provider ``yfinance`` declares phase 2.

Exit code 1 lists every problem (path and rule only, never the matched content).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SIGNATURES = Path(__file__).resolve().with_name("data_hygiene_signatures.json")
DEFAULT_FIXTURE_ROOTS = ("tests/fixtures", "platform/finplan_platform/data")
EXCLUDED_PARTS = {"calendars", "__pycache__"}
MARKET_KEYS = {"open", "high", "low", "close", "adj_close", "adj close", "volume", "observations", "rows", "dividends", "stock splits"}
LIVE_FLAG = re.compile(r"FINPLAN_LIVE_PROVIDER_TEST[\"']?\]?\s*(?:[:=]|,)\s*[\"']?(1|true|yes)\b", re.IGNORECASE)
#: the pytest marker registration documents how to opt in; it does not enable anything
DOC_LINE = re.compile(r"live_provider\s*:")
REAL_IMPORT = re.compile(r"^\s*(import\s+yfinance\b|from\s+yfinance\b)", re.MULTILINE)
SCAN_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".toml", ".sh", ".cfg", ".ini", ".txt"}
SKIP_DIRS = {".venv", "node_modules", "cdk.out", ".git", "__pycache__", ".ruff_cache", "contracts", "openspec", "vendor"}


@dataclass(frozen=True)
class Problem:
    path: str
    rule: str

    def __str__(self) -> str:
        return f"{self.path}: {self.rule}"


def load_signatures(path: Path = SIGNATURES) -> tuple[list[re.Pattern[str]], set[str]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [re.compile(m) for m in doc.get("markers", [])], {h.lower() for h in doc.get("row_sha256", [])}


def _keys(doc: Any) -> Iterator[str]:
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield str(k).lower()
            yield from _keys(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _keys(v)


def _rows(doc: Any, instrument: str | None = None) -> Iterator[tuple[str, str, Any]]:
    """(instrument, date, close) triples found anywhere in a fixture."""
    if isinstance(doc, dict):
        inst = doc.get("instrument_id") or doc.get("ticker") or instrument
        day = doc.get("session_date") or doc.get("Date") or doc.get("date")
        close = doc.get("close", doc.get("Close"))
        if inst and day and close is not None:
            yield str(inst), str(day)[:10], close
        for v in doc.values():
            yield from _rows(v, inst)
    elif isinstance(doc, list):
        for v in doc:
            yield from _rows(v, instrument)


def row_hash(instrument: str, day: str, close: Any) -> str:
    return hashlib.sha256(f"{instrument}|{day}|{close}".encode()).hexdigest()


def fixture_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.json")):
            if not EXCLUDED_PARTS.intersection(p.parts):
                yield p


def check_fixtures(roots: Iterable[Path], *, signatures: Path = SIGNATURES, base: Path = ROOT) -> list[Problem]:
    markers, hashes = load_signatures(signatures)
    problems: list[Problem] = []
    for path in fixture_files(roots):
        rel = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
        text = path.read_text(encoding="utf-8")
        for m in markers:
            if m.search(text):
                problems.append(Problem(rel, f"matches the real-data signature marker {m.pattern!r}"))
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            problems.append(Problem(rel, "is not valid JSON"))
            continue
        if not (MARKET_KEYS & set(_keys(doc))):
            continue
        if not (isinstance(doc, dict) and doc.get("synthetic") is True):
            problems.append(Problem(rel, "market-data fixture lacks a top-level \"synthetic\": true"))
        if hashes and any(row_hash(*r) in hashes for r in _rows(doc)):
            problems.append(Problem(rel, "contains a row matching the real-data hash list"))
    return problems


def _text_files(root: Path) -> Iterator[Path]:
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in SCAN_SUFFIXES and not SKIP_DIRS.intersection(p.relative_to(root).parts):
            yield p


def check_pipeline_suites(root: Path = ROOT, *, allowed: Iterable[str] = ("tests/integration/test_live_yfinance_shape.py", "scripts/check_data_hygiene.py")) -> list[Problem]:
    allowed_set = set(allowed)
    problems: list[Problem] = []
    for path in _text_files(root):
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel not in allowed_set and any(LIVE_FLAG.search(line) and not DOC_LINE.search(line) for line in text.splitlines()):
            problems.append(Problem(rel, "enables the opt-in live provider test (FINPLAN_LIVE_PROVIDER_TEST)"))
        if rel.startswith("tests/") and path.suffix == ".py" and REAL_IMPORT.search(text) and "live_provider" not in text:
            problems.append(Problem(rel, "imports the real yfinance library in a pipeline suite without the live_provider marker"))
    for cfg in sorted((root / "config").glob("*.json")) if (root / "config").is_dir() else []:
        try:
            doc = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        ingest = doc.get("ingest") if isinstance(doc, dict) else None
        if isinstance(ingest, dict) and ingest.get("provider") == "yfinance" and doc.get("phase") != 2:
            problems.append(Problem(str(cfg.relative_to(root)), "names the yfinance provider outside phase 2"))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--fixture-root", type=Path, action="append", help="fixture root (repeatable; default: tests/fixtures and platform data)")
    args = ap.parse_args(argv)
    roots = args.fixture_root or [args.root / r for r in DEFAULT_FIXTURE_ROOTS]
    problems = check_fixtures(roots, base=args.root) + check_pipeline_suites(args.root)
    for p in problems:
        print(f"FAIL: {p}", file=sys.stderr)
    if not problems:
        print("ok: market-data fixtures are synthetic and no pipeline suite enables the real provider")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
