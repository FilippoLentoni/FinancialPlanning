"""CS-11 (registered vocabularies), 12.1 D10 additions and 14.3 observation/flag additions."""

import copy

from finplan_contracts.validate import validate

from conftest import fixture

D10_QUALITY_FLAGS = {"no_session", "missing_sessions", "rejected_records", "source_revised", "contains_intraday_partial", "finality_inferred", "no_new_observations", "stale_source"}
D12_QUALITY_FLAGS = {"empty_response", "partial_response"}
ARTIFACT_KINDS = {"snapshot_manifest", "snapshot_payload", "plan_content", "plan_export", "excel_source", "staged_output_manifest", "validation_report", "import_report", "research_dataset", "run_artifact", "explanation_evidence"}


def test_budget_categories_closed(store):
    cats = store.get("budget-allocation").schema["$defs"]["category"]["enum"]
    assert cats == ["platform_infra", "cpu_research", "bedrock_explanations", "gpu", "reserve"]
    assert not validate(fixture("cost-estimate/invalid/unregistered-category-typesafe.json"), "cost-estimate").valid


def test_snapshot_status_closed(store):
    assert store.get("input-snapshot").schema["$defs"]["status"]["enum"] == ["committed", "approved", "expired"]
    assert not validate(fixture("input-snapshot/invalid/unknown-status.json"), "input-snapshot").valid


def test_quality_flags_open_enum_registers_d10_and_d12_values(store):
    qf = store.get("input-snapshot").schema["$defs"]["quality_flag"]
    assert qf["x-finplan-open-enum"] is True
    assert (D10_QUALITY_FLAGS | D12_QUALITY_FLAGS) == set(qf["x-finplan-known-values"])
    for flag in D10_QUALITY_FLAGS | D12_QUALITY_FLAGS:
        snap = fixture("input-snapshot/valid/approved-etf-daily.json")
        snap["quality_flags"] = [flag]
        assert validate(snap, "input-snapshot").valid, flag


def test_unknown_open_enum_values_pass():
    assert validate(fixture("input-snapshot/valid/unknown-quality-flag-from-later-minor.json"), "input-snapshot").valid
    assert validate(fixture("artifact-ref/valid/unknown-kind-from-later-minor.json"), "artifact-ref").valid


def test_artifact_kind_open_enum(store):
    kind = store.get("artifact-ref").schema["$defs"]["kind"]
    assert kind["x-finplan-open-enum"] is True and set(kind["x-finplan-known-values"]) == ARTIFACT_KINDS


def test_job_lifecycle_purpose_dry_run_and_cost_estimate(store):
    s = store.get("job-status").schema
    assert s["$defs"]["state"]["enum"] == ["awaiting_approval", "queued", "starting", "running", "stopping", "succeeded", "failed", "cancelled", "timed_out"]
    assert s["$defs"]["purpose"]["enum"] == ["research", "tuning", "holdout_evaluation", "production_candidate"]
    assert "dry_run" in s["properties"] and "dry_run" in store.get("job-submission").schema["properties"]
    ce = store.get("cost-estimate").schema
    assert set(ce["required"]) == {"estimated_usd_upper_bound", "price_retrieved_at", "remaining_allocation_usd", "budget_category"}


def test_non_terminal_state_has_no_completion_status():
    st = fixture("job-status/valid/awaiting-approval-gpu.json")
    assert st["state"] == "awaiting_approval" and "completion_status" not in st
    assert validate(st, "job-status").valid
    assert not validate(fixture("job-status/invalid/awaiting-approval-with-completion-status.json"), "job-status").valid
    assert not validate(fixture("job-status/invalid/terminal-without-completion-status.json"), "job-status").valid


def test_cost_allocation_tag_keys(store):
    props = set(store.get("cost-allocation-tags").schema["properties"])
    assert props == {"project", "owner-repo", "environment", "logical-role", "run-id"}


def test_daily_observation_optional_fields():
    obs = fixture("observation/valid/completed-daily.json")
    assert {"adj_close", "dividend", "split_ratio"} <= set(obs)
    minimal = fixture("observation/valid/schema-upgrade-without-optional-fields.json")
    assert not {"adj_close", "dividend", "split_ratio"} & set(minimal)
    assert validate(obs, "observation").valid and validate(minimal, "observation").valid


def test_phase2_minors_not_in_1_0(store):
    pv = set(store.get("plan-version").schema["properties"])
    assert not pv & {"solver", "solver_version", "tolerance", "seed", "code_version"}
    assert "portfolio-policy" not in store
    jr = set(store.get("run-result-payload").schema["properties"])
    assert not jr & {"leakage_risk", "out_of_configuration"}


def test_d10_schemas_present(store):
    for name in ("staged-output-manifest", "excel-plan-template", "import-report", "tool-catalog", "caller", "cost-estimate", "budget-allocation"):
        assert name in store


def test_caller_block_channels(store):
    assert store.get("caller").schema["properties"]["channel"]["enum"] == ["hosted_agent", "direct_mcp", "ci_test", "direct_test", "website", "scheduler", "operator"]
    assert validate(fixture("caller/valid/hosted-agent.json"), "caller").valid
    assert not validate(fixture("caller/invalid/unknown-channel.json"), "caller").valid


def test_tool_catalog_lists_every_published_tool_and_ids_resolve():
    from finplan_contracts.conformance import PUBLISHED_TOOLS

    cat = fixture("tool-catalog/valid/gamma-catalog.json")
    # the catalog fixture is a 1.0.0 fixture (kept byte-identical, CON-02); production_strategy is added in 1.1.0
    assert {t["name"] for t in cat["tools"]} == set(PUBLISHED_TOOLS) - {"production_strategy"}
    assert validate(cat, "tool-catalog").valid
    bad = copy.deepcopy(cat)
    bad["tools"][0]["input_schema_id"] = bad["tools"][0]["input_schema_id"].replace("-request", "-v9-request")
    assert not validate(bad, "tool-catalog").valid
