"""ENV-09: pipeline-structure check over synthesized pipeline templates (task 10.1)."""

from __future__ import annotations

import json

import pytest

from finplan_contracts import pipeline_check
from finplan_contracts.pipeline_check import check_pipeline_template, classify_stage

from infra_helpers import INFRA, infra, infra_files

EXPECTED = {
    "missing-approval.json": "missing manual approval stage",
    "gamma-before-beta.json": "out of order",
    "approval-after-prod.json": "out of order",
    "post-build-uses-source.json": "consumes the source checkout",
    "gamma-runs-cdk-synth.json": "runs cdk synth",
    "literal-connection-arn.json": "literal connection ARN",
    "prod-deploy-without-scoped-role.json": "scoped deploy role",
    "prod-without-smoke-tests.json": "no smoke tests",
    "v1-without-rollback-variable.json": "rollback_to_release_id",
    "source-not-main.json": "branch main",
    "deploy-as-caller.json": "literal role ARN",
}


def test_standard_pipeline_passes():
    assert check_pipeline_template(infra("pipelines/valid/standard.json")) == []


def test_stage_classification_is_source_build_beta_gamma_approval_prod():
    stages = infra("pipelines/valid/standard.json")["Resources"]["Pipeline"]["Properties"]["Stages"]
    assert [classify_stage(s) for s in stages] == list(pipeline_check.STAGE_ORDER)


@pytest.mark.parametrize("path", infra_files("pipelines/invalid"), ids=lambda p: p.name)
def test_invalid_pipelines_fail(path):
    findings = check_pipeline_template(json.loads(path.read_text()))
    assert findings, path.name
    if path.name in EXPECTED:
        assert any(EXPECTED[path.name] in str(f) for f in findings), [str(f) for f in findings]


def test_every_invalid_fixture_has_an_expectation():
    names = {p.name for p in infra_files("pipelines/invalid")}
    assert names == set(EXPECTED)


def test_template_without_pipeline_fails():
    assert check_pipeline_template({"Resources": {}})


def test_cli(capsys):
    assert pipeline_check.main([str(INFRA / "pipelines/valid/standard.json")]) == 0
    assert pipeline_check.main([str(INFRA / "pipelines/invalid/missing-approval.json")]) == 1
    assert "missing manual approval stage" in capsys.readouterr().err
