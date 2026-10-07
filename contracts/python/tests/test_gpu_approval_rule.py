"""ENV-20: a GPU job without a recorded user approval cannot leave awaiting_approval (task 13.5)."""

from __future__ import annotations

import json

import pytest

from finplan_contracts import conformance, gpu_rule, validate as validate_mod
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate

from conftest import CONTRACTS, FIXTURES
from infra_helpers import infra_files

VALID = infra_files("job-status/valid")
INVALID = infra_files("job-status/invalid")


@pytest.mark.parametrize("path", VALID + INVALID, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_fixtures_are_schema_valid_job_statuses(path):
    """Every ENV-20 fixture is a schema-valid job status, so only the GPU rule separates them.

    The rule is wired into ``validate`` (``x-finplan-checks``: ``gpu_approval_required``),
    so valid fixtures pass and invalid ones fail only on the GPU approval rule.
    """
    result = validate(json.loads(path.read_text()), "job-status")
    if path.parent.name == "valid":
        assert result.valid, result.issues
    else:
        assert not result.valid
        assert {i.keyword for i in result.issues} == {"x-finplan-gpu-approval"}, result.issues


def test_rule_is_wired_into_validation():
    store = load_store()
    for name in gpu_rule.JOB_STATUS_SCHEMAS:
        assert gpu_rule.CHECK_NAME in store.get(name).checks
    assert gpu_rule.CHECK_NAME in validate_mod.CHECKS
    assert gpu_rule.fixture_check in conformance.builtin_fixture_checks()


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_rule_accepts_valid(path):
    assert gpu_rule.violations(json.loads(path.read_text())) == []


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_rule_rejects_invalid(path):
    issues = gpu_rule.check_job_status(json.loads(path.read_text()))
    assert issues and all(i.code == "VALIDATION_FAILED" for i in issues)


def test_gpu_run_without_approval_stays_awaiting_approval():
    doc = json.loads((FIXTURES / "job-status/valid/awaiting-approval-gpu.json").read_text())
    assert gpu_rule.is_gpu_job(doc) and not gpu_rule.has_recorded_approval(doc)
    assert gpu_rule.violations(doc) == []
    for state in ("queued", "starting", "running"):
        assert not gpu_rule.may_transition(doc, state)
        assert gpu_rule.violations({**doc, "state": state})
    assert gpu_rule.may_transition(doc, "awaiting_approval")
    approved = json.loads((FIXTURES / "job-status/valid/approved-gpu-queued.json").read_text())
    assert gpu_rule.may_transition(approved, "starting") and gpu_rule.violations(approved) == []


def test_cpu_jobs_are_not_subject_to_the_rule():
    doc = json.loads((FIXTURES / "job-status/valid/running.json").read_text())
    assert not gpu_rule.is_gpu_job(doc) and gpu_rule.violations(doc) == []


def test_conformance_hook_passes_on_the_package_fixtures_and_catches_a_bad_one(tmp_path):
    paths = sorted(FIXTURES.rglob("*.json"))
    assert gpu_rule.fixture_check(CONTRACTS, paths) == []
    # a copy of the contracts fixture layout with a GPU fixture that left awaiting_approval unapproved
    bad_dir = tmp_path / "fixtures" / "job-status" / "valid"
    bad_dir.mkdir(parents=True)
    bad = bad_dir / "gpu-queued-unapproved.json"
    bad.write_text((infra_files("job-status/invalid")[0]).read_text())
    probs = gpu_rule.fixture_check(tmp_path, [bad])
    assert probs and probs[0].check == "gpu-approval"


def test_registration_hooks_are_idempotent_and_do_not_disturb_other_checks():
    before = list(conformance.EXTRA_FIXTURE_CHECKS)
    try:
        gpu_rule.register_conformance_hook()
        gpu_rule.register_conformance_hook()
        assert conformance.EXTRA_FIXTURE_CHECKS.count(gpu_rule.fixture_check) == 1
    finally:
        conformance.EXTRA_FIXTURE_CHECKS[:] = before
    had = gpu_rule.CHECK_NAME in validate_mod.CHECKS
    gpu_rule.register_semantic_check()
    try:
        fn = validate_mod.CHECKS[gpu_rule.CHECK_NAME]
        store = load_store()
        info = store.get("job-status")
        doc = json.loads(INVALID[0].read_text())
        ctx = validate_mod.CheckContext(document=doc, info=info, store=store, context={})
        assert fn(ctx)
    finally:
        if not had:
            validate_mod.CHECKS.pop(gpu_rule.CHECK_NAME, None)
