"""Platform pipeline structure and scoped deploy roles (task 10.1; PIPE-01, PIPE-03; contracts ENV-09, ENV-12).

The contract pipeline-structure check runs on the synthesized tooling template; the template is
also mutated to prove the check bites. Deploy and stage roles are simulated offline.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from finplan_contracts.bootstrap import check_deploy_roles
from finplan_contracts.pipeline_check import check_pipeline_template

from infra.policy_sim import Principal, simulate
from infra.stacks.pipeline import ENV_SUITES, ROLLBACK_VARIABLE, STAGE_NAMES, stage_spec
from tests.unit.ops.conftest import resources_of, tags_of


def _pipeline(template: dict[str, Any]) -> dict[str, Any]:
    (p,) = resources_of(template, "AWS::CodePipeline::Pipeline").values()
    return p["Properties"]


def _stage(template: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in _pipeline(template)["Stages"] if s["Name"] == name)


# ------------------------------------------------------------------ PIPE-01
def test_pipeline_passes_the_contract_structure_check_PIPE_01(tooling_template: dict[str, Any]) -> None:
    assert check_pipeline_template(tooling_template) == []
    assert check_deploy_roles(tooling_template) == []


def test_stage_order_type_and_rollback_variable_PIPE_01(tooling_template: dict[str, Any]) -> None:
    p = _pipeline(tooling_template)
    assert tuple(s["Name"] for s in p["Stages"]) == STAGE_NAMES
    assert p["PipelineType"] == "V2" and p["Name"] == "finplan-shared-financialplanning-pipeline"
    assert [v["Name"] for v in p["Variables"]] == [ROLLBACK_VARIABLE]


def test_source_is_main_through_the_ssm_connection_reference_PIPE_01(tooling_template: dict[str, Any]) -> None:
    (action,) = _stage(tooling_template, "Source")["Actions"]
    cfg = action["Configuration"]
    assert cfg["BranchName"] == "main" and cfg["FullRepositoryId"] == "FilippoLentoni/FinancialPlanning"
    param = tooling_template["Parameters"][cfg["ConnectionArn"]["Ref"]]
    assert param == {"Type": "AWS::SSM::Parameter::Value<String>", "Default": "/finplan/shared/financialplanning/config/codeconnection-ref"}


def test_deploy_order_and_scoped_roles_per_environment_PIPE_01(tooling_template: dict[str, Any]) -> None:
    roles = resources_of(tooling_template, "AWS::IAM::Role")
    for env, suite in ENV_SUITES.items():
        actions = _stage(tooling_template, env.capitalize())["Actions"]
        deploys = [a for a in actions if a["ActionTypeId"]["Category"] == "Deploy"]
        assert [a["Name"] for a in sorted(deploys, key=lambda a: a["RunOrder"])] == ["DeployStorage", "DeployMetadata", "DeployApi", "DeployIngestion"]
        for a in deploys:
            assert a["Configuration"]["StackName"].startswith(f"finplan-{env}-financialplanning-")
            role = roles[a["RoleArn"]["Fn::GetAtt"][0]]
            assert role["Properties"]["RoleName"] == f"finplan-shared-financialplanning-deploy-role-{env}"
            assert tags_of(role)["logical-role"] == "deploy-role" and role["Properties"]["PermissionsBoundary"]
            exec_role = roles[a["Configuration"]["RoleArn"]["Fn::GetAtt"][0]]
            assert exec_role["Properties"]["RoleName"] == f"finplan-shared-financialplanning-deploy-role-{env}-exec"
        last = max(a["RunOrder"] for a in deploys)
        after = sorted((a for a in actions if a["RunOrder"] > last), key=lambda a: a["RunOrder"])
        assert [a["Name"] for a in after] == ["PublishManifest", "".join(p.capitalize() for p in suite.split("-")) + "Tests"]


def test_approval_stage_is_one_manual_action_PIPE_01(tooling_template: dict[str, Any]) -> None:
    (action,) = _stage(tooling_template, "Approval")["Actions"]
    assert action["ActionTypeId"] == {"Category": "Approval", "Owner": "AWS", "Provider": "Manual", "Version": "1"}


def test_build_transition_stays_disabled_until_the_dry_run_passed_PIPE_01(tooling_template: dict[str, Any]) -> None:
    p = _pipeline(tooling_template)
    cond, passed, pending = p["DisableInboundStageTransitions"]["Fn::If"]
    assert cond == "SourceDryRunPassedCondition" and passed == {"Ref": "AWS::NoValue"}
    assert pending[0]["StageName"] == "Build"
    assert tooling_template["Parameters"]["SourceDryRunPassed"]["Default"] == "false"


def test_the_contract_check_rejects_a_pipeline_without_approval_PIPE_01(tooling_template: dict[str, Any]) -> None:
    broken = copy.deepcopy(tooling_template)
    lid = next(iter(resources_of(broken, "AWS::CodePipeline::Pipeline")))
    stages = broken["Resources"][lid]["Properties"]["Stages"]
    broken["Resources"][lid]["Properties"]["Stages"] = [s for s in stages if s["Name"] != "Approval"]
    assert any("approval" in str(f) for f in check_pipeline_template(broken))


# ------------------------------------------------------------------ PIPE-03 (artifact-only promotion)
def test_post_build_stages_consume_only_the_build_output_PIPE_03(tooling_template: dict[str, Any], ops_assembly: Path) -> None:
    p = _pipeline(tooling_template)
    (build,) = _stage(tooling_template, "Build")["Actions"]
    assert [o["Name"] for o in build["OutputArtifacts"]] == ["BuildOutput"]
    for stage in p["Stages"][2:]:
        for a in stage["Actions"]:
            assert [i["Name"] for i in a.get("InputArtifacts", [])] in ([], ["BuildOutput"]), (stage["Name"], a["Name"])
            if a["ActionTypeId"]["Category"] == "Deploy":
                path = a["Configuration"]["TemplatePath"]
                assert path.startswith("BuildOutput::cdk.out/assembly-")
                assert (ops_assembly / path.split("::cdk.out/", 1)[1]).is_file()
    projects = resources_of(tooling_template, "AWS::CodeBuild::Project")
    stage_specs = [json.loads(r["Properties"]["Source"]["BuildSpec"]) for r in projects.values() if "stage" in r["Properties"]["Name"]]
    assert len(stage_specs) == 3 and all(spec == stage_spec() for spec in stage_specs)
    assert "cdk synth" not in json.dumps(stage_spec()) and "synth.py" not in json.dumps(stage_spec())


def test_environment_templates_need_no_cdk_bootstrap_stack(ops_assembly: Path) -> None:
    for path in sorted(ops_assembly.glob("assembly-*/*.template.json")):
        t = json.loads(path.read_text())
        assert "BootstrapVersion" not in (t.get("Parameters") or {}) and "CheckBootstrapVersion" not in (t.get("Rules") or {}), path.name
        for r in resources_of(t, "AWS::Lambda::Function").values():
            code = r["Properties"]["Code"]
            assert code["S3Bucket"] == {"Fn::Sub": "finplan-shared-financialplanning-pipeline-store-${AWS::AccountId}"}
            assert code["S3Key"].startswith("assets/")


# ------------------------------------------------------------------ scoped roles (simulation)
def _role(resolver: Any, name: str) -> Principal:
    from finplan_contracts.boundaries import shared_permission_boundary

    return Principal.role(name, *resolver.role_policies(name), boundary=shared_permission_boundary())


def test_cfn_execution_role_is_scoped_to_its_environment(tooling_resolver: Any) -> None:
    beta = _role(tooling_resolver, "finplan-shared-financialplanning-deploy-role-beta-exec")
    assert simulate("s3:CreateBucket", "arn:aws:s3:::finplan-beta-financialplanning-raw-<account-id>", beta).allowed
    assert not simulate("s3:CreateBucket", "arn:aws:s3:::finplan-gamma-financialplanning-raw-<account-id>", beta).allowed
    assert simulate("dynamodb:CreateTable", "arn:aws:dynamodb:us-east-2:<account-id>:table/finplan-beta-financialplanning-plan", beta).allowed
    assert not simulate("dynamodb:CreateTable", "arn:aws:dynamodb:us-east-2:<account-id>:table/finplan-prod-financialplanning-plan", beta).allowed
    role = "arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-plan-api-handler-role"
    beta_boundary = "arn:aws:iam::<account-id>:policy/finplan-beta-permission-boundary"
    assert simulate("iam:CreateRole", role, beta, context={"iam:PermissionsBoundary": beta_boundary}).allowed
    assert not simulate("iam:CreateRole", role, beta).allowed  # no boundary
    assert not simulate("iam:CreateRole", role, beta, context={"iam:PermissionsBoundary": beta_boundary.replace("beta", "prod")}).allowed
    param = "arn:aws:ssm:us-east-2:<account-id>:parameter/finplan/{}/financialplanning/config/bucket-raw"
    assert simulate("ssm:PutParameter", param.format("beta"), beta).allowed
    assert not simulate("ssm:PutParameter", param.format("gamma"), beta).allowed
    assert not simulate("ssm:PutParameter", "arn:aws:ssm:us-east-2:<account-id>:parameter/finplan/shared/financialplanning/config/budget-state", beta).allowed


def test_stage_role_publishes_only_its_own_release_keys(tooling_resolver: Any) -> None:
    gamma = _role(tooling_resolver, "finplan-gamma-financialplanning-operator-pipeline-stage")
    p = "arn:aws:ssm:us-east-2:<account-id>:parameter/finplan/{}/financialplanning/{}"
    assert simulate("ssm:PutParameter", p.format("gamma", "release/manifest"), gamma).allowed
    assert simulate("ssm:PutParameter", p.format("gamma", "release/current-release-id"), gamma).allowed
    assert simulate("ssm:PutParameter", p.format("gamma", "config/budget-enforced-role-names"), gamma).allowed
    assert not simulate("ssm:PutParameter", p.format("prod", "release/manifest"), gamma).allowed
    assert not simulate("ssm:PutParameter", p.format("gamma", "api/plan-endpoint"), gamma).allowed
    assert simulate("ssm:GetParameter", p.format("gamma", "api/plan-endpoint"), gamma).allowed
    assert not simulate("ssm:GetParameter", p.format("prod", "api/plan-endpoint"), gamma).allowed
