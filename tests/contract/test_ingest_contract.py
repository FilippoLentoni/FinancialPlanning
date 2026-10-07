"""Contract tests for ingestion results and snapshot reads (ING-08, ING-16; contracts 0.2.0 pinned, 1.0.0 pending).

Results validate against ``tools/refresh-market-data-response``; snapshot records and their
lineage (``provider``, ``provider_library``, ``library_version``, ``retrieved_at``) against
``core/v1/input-snapshot``; observation reads against ``tools/query-market-data-response``.
Responses carry no storage location (contract ``no_storage_locations``/``no_leaks`` checks
through the validators). The package's own input-snapshot fixtures are read by the platform's
approval rule without error (older records without the provider-lineage fields included).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate
from finplan_platform.core.config import load_config
from finplan_platform.core.snapshots import evaluate_approval

# the ingestion fixtures (make_deps, octx, ...) are shared with the unit suite
from tests.unit.ingestion.conftest import FakeYF, config_with, make_deps, octx, on_demand  # noqa: F401


def test_fixture_result_and_read_validate(make_deps: Any, octx: Any) -> None:  # noqa: F811 - shared fixtures
    from finplan_platform.core.ingestion import run_ingestion
    from finplan_platform.core.snapshots import get_snapshot, read_observations

    deps = make_deps()
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    assert validate(res, "tools/refresh-market-data-response").valid
    read = get_snapshot(octx(), res["input_snapshot_id"], deps=deps)
    assert validate(read["snapshot"], "input-snapshot").valid
    assert read["snapshot"]["manifest_checksum"] == res["snapshot"]["manifest_checksum"]
    obs = read_observations(octx(), res["input_snapshot_id"], deps=deps)
    core = {k: obs[k] for k in ("snapshot", "observation_kinds", "instruments", "partial", "data_refs", "next_token")}
    assert validate(core, "tools/query-market-data-response").valid
    for o in obs["observations"]:
        assert validate(o, "observation").valid


def test_yfinance_lineage_fields_on_result_and_read(make_deps: Any, octx: Any, clock: Any) -> None:  # noqa: F811 - shared fixtures
    from finplan_platform.core.ingestion import run_ingestion
    from finplan_platform.core.snapshots import get_snapshot
    from finplan_platform.providers.yfinance_provider import YFinanceProvider

    cfg = config_with(**{"phase": 2, "ingest.provider": "yfinance"})
    p = YFinanceProvider(dataset_id=cfg.dataset_id, ticker="SPY", settings=cfg.ingest["provider_settings"], clock=clock, sleep=lambda s: clock.advance(seconds=s), library=FakeYF())
    deps = make_deps(provider=p, cfg=cfg)
    res = run_ingestion(octx(), on_demand("2026-01-02", "2026-01-09"), deps=deps)
    for snap in (res["snapshot"], get_snapshot(octx(), res["input_snapshot_id"], deps=deps)["snapshot"]):
        assert validate(snap, "input-snapshot").valid
        lin = snap["lineage"]
        assert lin["provider"] == "yfinance" and lin["provider_library"] == "yfinance"
        assert lin["library_version"] == "1.7.0" and lin["retrieved_at"].endswith("Z")
        assert lin["calendar_version"].startswith("xnys-exchange_calendars-")


@pytest.mark.parametrize("fixture", sorted(p.name for p in (load_store().fixtures_dir("input-snapshot") / "valid").glob("*.json")))
def test_contract_snapshot_fixtures_are_readable_by_the_approval_rule(fixture: str) -> None:
    doc = json.loads((load_store().fixtures_dir("input-snapshot") / "valid" / fixture).read_text())
    approved, _blocking, version = evaluate_approval(doc, load_config("beta"))
    assert version == "approval-v1"
    if {"empty_response", "stale_source"} & set(doc["quality_flags"]):
        assert not approved
