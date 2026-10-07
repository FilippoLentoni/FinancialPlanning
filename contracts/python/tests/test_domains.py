"""DOM-01..DOM-04: domain-neutral envelope with finance as the only registered adapter."""

from finplan_contracts.validate import validate

from conftest import fixture


def test_registry_has_exactly_finance(store):
    reg = store.domain_registry()
    assert [d["domain"] for d in reg["domains"]] == ["finance"]
    for schema_id in reg["domains"][0]["payload_schemas"].values():
        assert store.get(schema_id).namespace == "finance"


def test_finance_job_submission_envelope_and_payload_validate():
    """DOM-01: the envelope validates against core and the payload against the finance adapter."""
    sub = fixture("job-submission/valid/finance-research-cpu.json")
    assert sub["domain"] == "finance" and validate(sub, "job-submission").valid
    assert validate(sub["configuration"]["payload"], "experiment-config").valid
    res = validate(fixture("job-submission/invalid/finance-payload-invalid.json"), "job-submission")
    assert res.code == "VALIDATION_FAILED" and res.primary().pointer.startswith("/configuration/payload")


def test_unregistered_domain_rejected_naming_domain():
    """DOM-02: supply_chain is rejected with VALIDATION_FAILED naming the unknown domain."""
    for schema, name in (
        ("job-submission", "supply-chain-domain"),
        ("domain-envelope", "unregistered-supply-chain"),
        ("configuration", "unregistered-domain"),
        ("input-snapshot", "unregistered-domain"),
        ("tools/submit-experiment-request", "supply-chain-domain"),
    ):
        res = validate(fixture(f"{schema}/invalid/{name}.json"), schema)
        assert res.code == "VALIDATION_FAILED", (schema, name)
        assert any("supply_chain" in i.message and i.field == "domain" for i in res.issues), (schema, name)


def test_unserved_domain_schema_version_rejected():
    assert not validate(fixture("domain-envelope/invalid/unserved-domain-schema-version.json"), "domain-envelope").valid


def test_finance_field_in_envelope_rejected():
    assert not validate(fixture("job-submission/invalid/ticker-in-envelope.json"), "job-submission").valid
    assert not validate(fixture("domain-envelope/invalid/ticker-in-envelope.json"), "domain-envelope").valid


def test_observation_kind_lives_in_finance_adapter(store):
    """DOM-03: completed_daily/intraday_partial is a finance field; the envelope has only neutral coverage and flags."""
    assert store.get("observation").schema["properties"]["kind"]["enum"] == ["completed_daily", "intraday_partial"]
    assert store.get("observation").namespace == "finance"
    snap_props = set(store.get("input-snapshot").schema["properties"])
    assert {"coverage", "quality_flags"} <= snap_props and "kind" not in snap_props and "observations" not in snap_props
    payload = fixture("snapshot-payload/valid/etf-daily.json")
    assert all(o["kind"] in ("completed_daily", "intraday_partial") for o in payload["observations"])
    assert validate(payload, "snapshot-payload").valid


def test_explanation_evidence_separate_from_narrative():
    """DOM-04: evidence references with checksums plus a separate narrative."""
    res = fixture("explanation-result/valid/evidence-and-narrative.json")
    assert validate(res, "explanation-result").valid
    assert res["evidence"] and all(e["checksum"].startswith("sha256:") and e["kind"] == "explanation_evidence" for e in res["evidence"])
    assert not validate(fixture("explanation-result/invalid/narrative-only.json"), "explanation-result").valid
    assert validate(fixture("explanation-evidence/valid/allocation-delta.json"), "explanation-evidence").valid
