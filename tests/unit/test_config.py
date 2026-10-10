"""Environment configuration schema (task 1.3): ING-02, ING-10, ING-13, STO-06 config rules, leak scan."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from finplan_contracts import leak_scan

from finplan_platform.core.config import (
    ConfigError,
    EnvConfig,
    config_dir,
    load_all,
    load_config,
    load_shared_config,
    schedule_expression,
    validate_config,
)


def _doc(env: str = "beta") -> dict[str, Any]:
    return json.loads((config_dir() / f"{env}.json").read_text())


def _problems(doc: dict[str, Any], env: str | None = None) -> list[tuple[str, str | None]]:
    return [(p.pointer, p.test_id) for p in validate_config(doc, env=env)]


def test_committed_configs_are_valid() -> None:
    cfgs = load_all()
    assert set(cfgs) == {"beta", "gamma", "prod"}
    for env, cfg in cfgs.items():
        # decision 26: every environment runs the same real-data configuration; the phase is the
        # only switch (yfinance in phase 2, promoted beta -> gamma -> prod by the UNI-06 gate)
        assert cfg.env == env and cfg.region == "us-east-2" and cfg.phase in (1, 2)
        assert cfg.provider == ("yfinance" if cfg.phase == 2 else "fixture") and cfg.schedule_time == "09:00"
        assert cfg.dataset_id == "finance/etf-daily/SPY"
        assert cfg.metadata["idempotency_ttl_days"] >= 7 and cfg.metadata["orphan_grace_hours"] >= 24


def test_default_schedule_is_0900_and_cron_follows_configuration() -> None:
    assert schedule_expression("09:00") == "cron(0 9 ? * MON-FRI *)"
    assert schedule_expression("09:30") == "cron(30 9 ? * MON-FRI *)"


@pytest.mark.parametrize("value", ["10:00", "9:00", "09:15", "", "0900"])
def test_invalid_schedule_value_rejected_ING_02(value: str) -> None:
    doc = _doc()
    doc["ingest"]["schedule_time"] = value
    assert ("/ingest/schedule_time", "ING-02") in _problems(doc)
    with pytest.raises(ConfigError):
        schedule_expression(value)


@pytest.mark.parametrize("provider", ["yfinance", "stooq", "mock"])
def test_phase1_non_fixture_provider_rejected_ING_10(provider: str) -> None:
    doc = _doc()
    doc["phase"] = 1
    doc["ingest"]["provider"] = provider
    assert ("/ingest/provider", "ING-10") in _problems(doc)


def test_phase2_allows_yfinance_but_not_others_ING_10() -> None:
    doc = _doc()
    doc["phase"] = 2
    doc["ingest"]["provider"] = "yfinance"
    assert _problems(doc) == []
    doc["ingest"]["provider"] = "stooq"
    assert ("/ingest/provider", "ING-10") in _problems(doc)


@pytest.mark.parametrize("granularity", ["intraday", "1m", "hourly"])
def test_intraday_granularity_rejected_ING_13(granularity: str) -> None:
    doc = _doc()
    doc["ingest"]["dataset"]["granularity"] = granularity
    assert ("/ingest/dataset/granularity", "ING-13") in _problems(doc)


def test_only_etf_daily_dataset_enabled_ING_13() -> None:
    doc = _doc()
    doc["ingest"]["dataset"]["kind"] = "index-level"
    assert ("/ingest/dataset/kind", "ING-13") in _problems(doc)


def test_beta_and_prod_retain_history_while_gamma_expires_STO_06() -> None:
    doc = _doc("gamma")
    doc["retention"]["plans_days"] = None
    assert ("/retention/plans_days", "STO-06") in _problems(doc)
    assert load_config("beta").retention_days("plans") is None
    prod = load_config("prod")
    assert prod.retention_days("plans") is None and prod.retention_days("snapshots") is None
    assert prod.retention["staging_window_days"] == 7


def test_retry_budget_must_fit_function_timeout_ING_17() -> None:
    doc = _doc()
    doc["ingest"]["provider_settings"].update(max_attempts=10, backoff_max_seconds=60, backoff_initial_seconds=30)
    assert any(t == "ING-17" for _, t in _problems(doc))


def test_consumer_principals_must_name_the_same_environment() -> None:
    doc = _doc("gamma")
    doc["consumer_principals"]["financemodel_job"]["ssm_key"] = "/finplan/prod/financemodel/job/job-role-ref"
    doc["consumer_principals"]["tool_reader"]["role_name_pattern"] = "finplan-prod-financelambdastool-tool-role-reader*"
    pointers = [p for p, _ in _problems(doc, env="gamma")]
    assert "/consumer_principals/financemodel_job/ssm_key" in pointers
    assert "/consumer_principals/tool_reader/role_name_pattern" in pointers


def test_financemodel_role_references_use_the_registered_contract_keys() -> None:
    """Contracts 0.2.0 registers financemodel/job/job-role-ref and job-api-role-ref (D13)."""
    from finplan_contracts import ssm as contract_ssm

    for env in ("beta", "gamma", "prod"):
        cps = _doc(env)["consumer_principals"]
        for name, key in (("financemodel_job", "job-role-ref"), ("financemodel_job_api", "job-api-role-ref")):
            assert cps[name]["ssm_key"] == f"/finplan/{env}/financemodel/job/{key}" and cps[name]["registered"] is True
            assert contract_ssm.find_registered(cps[name]["ssm_key"]) is not None


def test_a_reference_marked_registered_must_be_a_contract_key() -> None:
    doc = _doc()
    doc["consumer_principals"]["financemodel_job"]["ssm_key"] = "/finplan/beta/financemodel/job/not-a-key"
    assert "/consumer_principals/financemodel_job/ssm_key" in [p for p, _ in _problems(doc)]
    doc["consumer_principals"]["financemodel_job"]["registered"] = False  # a proposed key is allowed
    assert "/consumer_principals/financemodel_job/ssm_key" not in [p for p, _ in _problems(doc)]


def test_identifiers_are_rejected_as_principal_references() -> None:
    doc = _doc()
    doc["consumer_principals"]["operator"]["role_name_pattern"] = "arn:aws:iam::<account-id>:role/admin"
    doc["consumer_principals"]["operator"]["ssm_key"] = "arn:aws:ssm:us-east-2:<account-id>:parameter/x"
    pointers = [p for p, _ in _problems(doc)]
    assert "/consumer_principals/operator/role_name_pattern" in pointers and "/consumer_principals/operator/ssm_key" in pointers


def test_unknown_fields_rejected() -> None:
    doc = _doc()
    doc["bucket_name"] = "anything"
    assert _problems(doc)


def test_file_environment_mismatch_rejected(tmp_path: Path) -> None:
    doc = _doc("beta")
    (tmp_path / "gamma.json").write_text(json.dumps(doc))
    with pytest.raises(ConfigError):
        load_config("gamma", tmp_path)


def test_shared_config_holds_only_the_ceiling_default() -> None:
    shared = load_shared_config()
    assert shared["cost_ceiling_usd_default"] == 50
    assert set(shared) <= {"$comment", "region", "cost_ceiling_usd_default"}


def test_leak_scan_passes_on_config() -> None:
    count, findings = leak_scan.scan_paths([config_dir()])
    assert count >= 4
    assert findings == [], [str(f) for f in findings]


def test_env_config_view_is_read_only_mapping() -> None:
    cfg = load_config("beta")
    assert isinstance(cfg, EnvConfig)
    assert cfg.principal_pattern("financemodel_job").startswith("finplan-beta-")
    with pytest.raises(KeyError):
        cfg.retention_days("staging")
    _ = copy.deepcopy(cfg.data)
