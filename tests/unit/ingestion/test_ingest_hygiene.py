"""Data hygiene and public-repo hygiene of the ingestion files (task 6.16; ING-19, leak scan for 6.10).

ING-19: the build-stage data-hygiene check passes on the repository and fails on a
non-synthetic fixture, a fixture matching the real-data signature list, a pipeline suite that
enables the live provider test, and a pipeline test that imports the real library unmarked.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from finplan_contracts import leak_scan

from scripts import check_data_hygiene as hygiene

ROOT = Path(__file__).resolve().parents[3]
INGEST_PATHS = (
    "platform/finplan_platform/core/calendar.py",
    "platform/finplan_platform/core/ingestion.py",
    "platform/finplan_platform/core/ingestion_budget.py",
    "platform/finplan_platform/core/ingestion_datasets.py",
    "platform/finplan_platform/core/ingestion_normalize.py",
    "platform/finplan_platform/core/snapshots.py",
    "platform/finplan_platform/providers",
    "platform/finplan_platform/handlers/ingest.py",
    "platform/finplan_platform/handlers/scheduler.py",
    "platform/finplan_platform/data/calendars",
    "infra/stacks/ingestion.py",
    "infra/docker/ingestion",
    "scripts/generate_calendar.py",
    "scripts/check_ingest_pins.py",
    "scripts/check_data_hygiene.py",
    "scripts/data_hygiene_signatures.json",
    "scripts/ingestion_package_size.py",
    "tests/unit/ingestion",
    "tests/fixtures/market_data",
    "tests/integration/test_live_yfinance_shape.py",
    "tests/contract/test_ingest_contract.py",
    "docs/ingestion.md",
)


def test_repository_passes_the_data_hygiene_check() -> None:
    roots = [ROOT / r for r in hygiene.DEFAULT_FIXTURE_ROOTS]
    assert hygiene.check_fixtures(roots, base=ROOT) == []
    assert hygiene.check_pipeline_suites(ROOT) == []
    assert hygiene.main(["--root", str(ROOT)]) == 0


def test_non_synthetic_fixture_fails(tmp_path: Path) -> None:
    (tmp_path / "bars.json").write_text(json.dumps({"rows": [{"Date": "2026-01-05", "Close": 100.0}]}))
    (tmp_path / "ok.json").write_text(json.dumps({"synthetic": True, "rows": [{"Date": "2026-01-05", "Close": 100.0}]}))
    (tmp_path / "flag-false.json").write_text(json.dumps({"synthetic": False, "observations": []}))
    problems = hygiene.check_fixtures([tmp_path], base=tmp_path)
    assert sorted(p.path for p in problems) == ["bars.json", "flag-false.json"]


def test_fixture_matching_a_real_data_marker_fails_even_if_flagged(tmp_path: Path) -> None:
    (tmp_path / "raw.json").write_text(json.dumps({"synthetic": True, "chart": {"result": [{"meta": {"regularMarketPrice": 1}}]}}))
    problems = hygiene.check_fixtures([tmp_path], base=tmp_path)
    assert problems and all("signature marker" in p.rule for p in problems)


def test_fixture_matching_a_real_row_hash_fails(tmp_path: Path) -> None:
    sig = tmp_path / "sig.json"
    sig.write_text(json.dumps({"markers": [], "row_sha256": [hashlib.sha256(b"SPY|2026-01-05|123.45").hexdigest()]}))
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "a.json").write_text(json.dumps({"synthetic": True, "observations": [{"instrument_id": "SPY", "session_date": "2026-01-05", "close": 123.45}]}))
    problems = hygiene.check_fixtures([fx], signatures=sig, base=tmp_path)
    assert [p.rule for p in problems] == ["contains a row matching the real-data hash list"]


def test_pipeline_suite_enabling_the_live_provider_fails(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "buildspec.yml").write_text("env:\n  variables:\n    FINPLAN_LIVE_PROVIDER_TEST: \"1\"\n")
    (tmp_path / "tests" / "test_x.py").write_text("import yfinance\n\ndef test_x():\n    pass\n")
    (tmp_path / "tests" / "test_marked.py").write_text("import pytest\nimport yfinance\npytestmark = pytest.mark.live_provider\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "beta.json").write_text(json.dumps({"phase": 1, "ingest": {"provider": "yfinance"}}))
    rules = {(p.path, p.rule.split(" (")[0]) for p in hygiene.check_pipeline_suites(tmp_path, allowed=())}
    assert ("buildspec.yml", "enables the opt-in live provider test") in rules
    assert any(path == "tests/test_x.py" for path, _ in rules)
    assert not any(path == "tests/test_marked.py" for path, _ in rules)
    assert any(path == "config/beta.json" for path, _ in rules)


def test_leak_scan_passes_on_ingestion_files_and_docs() -> None:
    paths = [ROOT / p for p in INGEST_PATHS if (ROOT / p).exists()]
    count, findings = leak_scan.scan_paths(paths)
    assert count >= 20
    assert findings == [], [str(f) for f in findings]


def test_ingestion_doc_states_no_price_and_records_the_decisions() -> None:
    text = (ROOT / "docs/ingestion.md").read_text()
    for needle in ("OQ-5", "PQ-5", "RESOLVED 2026-10-07", "PQ-6", "yfinance", "XNYS", "exchange_calendars", "unofficial", "no API key", "personal/research", "synthetic"):
        assert needle in text, needle
    import re

    assert not re.search(r"(USD|\$)\s?[0-9]", text), "the document must not state a price"
