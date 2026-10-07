"""Bootstrap entry point with mocked STS, connection, pipeline and pricing (task 10.5; PIPE-07) and the
default budget allocation writer (task 9.1a; COST-06).

The whole sequence runs offline: fake clients record every call, SSM is moto, and the deploy
runner is a fake (no ``cdk deploy``, no AWS mutation). The account value is a non-numeric
placeholder so no account identifier appears in this file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from finplan_contracts.bootstrap import (
    EXTEND_INSTALLATION_MESSAGE,
    ROOT_RECOMMENDATION,
    BootstrapConfig,
    BootstrapStop,
    Clients,
)
from finplan_contracts.budget import (
    ALLOCATION_PARAMETER,
    CEILING_PARAMETER,
    DEFAULT_ALLOCATION,
)

from scripts import bootstrap

ACCOUNT = "acct-placeholder"
CONFIG = BootstrapConfig(
    account_id=ACCOUNT,
    primary_region="us-east-2",
    repo="financialplanning",
    codeconnection_arn="<codeconnection-arn>",
    github_repository="FilippoLentoni/FinancialPlanning",
    pipeline_name="finplan-shared-financialplanning-pipeline",
)


class FakeSts:
    def __init__(self, account: str = ACCOUNT, root: bool = True) -> None:
        self.account, self.root = account, root

    def get_caller_identity(self) -> dict[str, str]:
        acct = self.account
        arn = f"arn:aws:iam::{acct}:root" if self.root else f"arn:aws:iam::{acct}:user/operator"
        return {"Account": self.account, "Arn": arn, "UserId": "synthetic"}


class FakeConnections:
    def __init__(self, status: str = "AVAILABLE") -> None:
        self.status = status

    def get_connection(self, ConnectionArn: str) -> dict[str, Any]:
        return {"Connection": {"ConnectionStatus": self.status}}


class FakePipeline:
    def __init__(self, source_status: str = "Succeeded") -> None:
        self.source_status = source_status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def disable_stage_transition(self, **kw: Any) -> None:
        self.calls.append(("disable", kw))

    def enable_stage_transition(self, **kw: Any) -> None:
        self.calls.append(("enable", kw))

    def start_pipeline_execution(self, **kw: Any) -> dict[str, str]:
        self.calls.append(("start", kw))
        return {"pipelineExecutionId": "exec-1"}

    def get_pipeline_state(self, **kw: Any) -> dict[str, Any]:
        return {"stageStates": [{"stageName": "Source", "latestExecution": {"pipelineExecutionId": "exec-1", "status": self.source_status}}]}


class FakePricing:
    """Price List API shape; the unit prices are synthetic test values, not AWS prices."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_products(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        unit = {"AWSCodePipeline": "minute", "CodeBuild": "minute", "AmazonS3": "GB-Mo", "AWSLambda": "Requests", "AmazonSNS": "Requests", "AWSBudgets": "Budget-Day"}.get(kw["ServiceCode"], "none")
        product = {"terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": "0.001"}}}}}}}
        return {"PriceList": [json.dumps(product)]}


class FakeRunner:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: str, env: dict[str, str]) -> Any:
        self.calls.append(cmd)
        return type("P", (), {"returncode": self.rc})()


def _clients(ssm: Any, **kw: Any) -> Clients:
    return Clients(sts=kw.get("sts", FakeSts()), codeconnections=kw.get("conn", FakeConnections()), ssm=ssm, codepipeline=kw.get("pipeline", FakePipeline()), pricing=kw.get("pricing", FakePricing()))


def _run(ops_assembly: Path, tmp_path: Path, ssm: Any, *, runner: FakeRunner | None = None, approve: Any = None, out: list[str] | None = None, **kw: Any) -> Any:
    lines = out if out is not None else []
    return bootstrap.run(
        CONFIG,
        _clients(ssm, **kw),
        session_region=kw.get("region", "us-east-2"),
        assembly=kw.get("assembly", ops_assembly),
        bootstrap_dir=tmp_path / "cdk.out.bootstrap",
        approve=approve or (lambda plan: True),
        notification_email="person@example.invalid",
        runner=runner or FakeRunner(),
        record_path=tmp_path / "dry-run.json",
        sleep=lambda _s: None,
        out=lines.append,
    )


# ------------------------------------------------------------------ PIPE-07
def test_root_session_bootstraps_the_tooling_stacks_only_PIPE_07(ops_assembly: Path, tmp_path: Path, ssm: Any) -> None:
    runner, pipeline, lines = FakeRunner(), FakePipeline(), []
    report = _run(ops_assembly, tmp_path, ssm, runner=runner, pipeline=pipeline, out=lines)
    assert report.completed and report.caller_is_root
    assert lines.count(ROOT_RECOMMENDATION) == 2  # printed at the caller check and again at the end
    # exact stacks and the estimate are printed before anything is deployed
    plan_idx = next(i for i, line in enumerate(lines) if line.startswith("Stacks to deploy (exact):"))
    deploy_idx = next(i for i, line in enumerate(lines) if line.startswith("running: npx"))
    assert plan_idx < deploy_idx
    plan_text = lines[plan_idx]
    assert "finplan-shared-financialplanning-pipeline-store" in plan_text and "finplan-shared-financialplanning-tooling" in plan_text
    assert "Cost estimate" in plan_text and "finplan-beta" not in plan_text
    assert report.plan.stack_names == ["finplan-shared-financialplanning-pipeline-store", "finplan-shared-financialplanning-tooling"]
    # one cdk deploy, of the filtered assembly only, with the human-supplied address and the dry-run flag
    (cmd,) = runner.calls
    assert cmd[:4] == ["npx", "--yes", "aws-cdk@2", "deploy"] and "--all" in cmd
    assert cmd[cmd.index("--app") + 1] == str(tmp_path / "cdk.out.bootstrap")
    assert "finplan-shared-financialplanning-tooling:SourceDryRunPassed=false" in cmd
    assert "finplan-shared-financialplanning-tooling:ScopeBudgetToProjectTag=false" in cmd  # default: whole account
    assert "finplan-shared-financialplanning-tooling:NotificationEmail=person@example.invalid" in cmd
    assert not any("NotificationEmail=person" in line for line in lines)  # never echoed
    filtered = json.loads((tmp_path / "cdk.out.bootstrap" / "manifest.json").read_text())
    stacks = [a["properties"]["stackName"] for a in filtered["artifacts"].values() if a["type"] == "aws:cloudformation:stack"]
    assert sorted(stacks) == sorted(bootstrap.BOOTSTRAP_STACKS)
    # connection reference, ceiling and default allocation written; source dry run enabled the stages
    assert ssm.get_parameter(Name="/finplan/shared/financialplanning/config/codeconnection-ref")["Parameter"]["Value"] == "<codeconnection-arn>"
    assert float(ssm.get_parameter(Name=CEILING_PARAMETER)["Parameter"]["Value"]) == 50
    assert json.loads(ssm.get_parameter(Name=ALLOCATION_PARAMETER)["Parameter"]["Value"]) == DEFAULT_ALLOCATION
    assert [c[0] for c in pipeline.calls] == ["disable", "start", "enable"]
    assert json.loads((tmp_path / "dry-run.json").read_text())["deploy_stages_enabled"] is True


def test_dry_run_failure_stops_with_the_extend_installation_message_PIPE_07(ops_assembly: Path, tmp_path: Path, ssm: Any) -> None:
    pipeline = FakePipeline(source_status="Failed")
    with pytest.raises(BootstrapStop) as ei:
        _run(ops_assembly, tmp_path, ssm, pipeline=pipeline)
    assert ei.value.step == "source-dry-run"
    assert ei.value.message == EXTEND_INSTALLATION_MESSAGE.format(repository="FilippoLentoni/FinancialPlanning")
    assert ("enable", {"pipelineName": "finplan-shared-financialplanning-pipeline", "stageName": "Build", "transitionType": "Inbound"}) not in pipeline.calls


def test_account_mismatch_stops_before_anything_is_created_PIPE_07(ops_assembly: Path, tmp_path: Path, ssm: Any) -> None:
    runner = FakeRunner()
    with pytest.raises(BootstrapStop) as ei:
        _run(ops_assembly, tmp_path, ssm, runner=runner, sts=FakeSts(account="another-account"))
    assert ei.value.step == "caller" and runner.calls == []
    assert ssm.describe_parameters()["Parameters"] == []


def test_refuses_without_a_synthesized_assembly_PIPE_07(tmp_path: Path, ssm: Any) -> None:
    runner, pricing = FakeRunner(), FakePricing()
    with pytest.raises(BootstrapStop) as ei:
        _run(tmp_path / "no-assembly", tmp_path, ssm, runner=runner, pricing=pricing, assembly=tmp_path / "no-assembly")
    assert ei.value.step == "prerun" and "not synthesized" in ei.value.message
    assert runner.calls == [] and pricing.calls == []


def test_operator_declines_nothing_is_deployed_PIPE_07(ops_assembly: Path, tmp_path: Path, ssm: Any) -> None:
    runner = FakeRunner()
    with pytest.raises(BootstrapStop) as ei:
        _run(ops_assembly, tmp_path, ssm, runner=runner, approve=lambda plan: False)
    assert ei.value.step == "approval" and runner.calls == []
    assert ssm.describe_parameters()["Parameters"] == []


def test_wrong_region_and_unavailable_connection_stop_PIPE_07(ops_assembly: Path, tmp_path: Path, ssm: Any) -> None:
    with pytest.raises(BootstrapStop) as ei:
        _run(ops_assembly, tmp_path, ssm, region="eu-west-1")
    assert ei.value.step == "region"
    with pytest.raises(BootstrapStop) as ei:
        _run(ops_assembly, tmp_path, ssm, conn=FakeConnections("PENDING"))
    assert ei.value.step == "connection"


def test_failed_cdk_deploy_stops_before_the_dry_run(ops_assembly: Path, tmp_path: Path, ssm: Any) -> None:
    pipeline = FakePipeline()
    with pytest.raises(BootstrapStop) as ei:
        _run(ops_assembly, tmp_path, ssm, runner=FakeRunner(rc=1), pipeline=pipeline)
    assert ei.value.step == "deploy" and pipeline.calls == []


def test_rerun_after_a_passed_dry_run_keeps_the_stages_enabled(ops_assembly: Path, tmp_path: Path, ssm: Any) -> None:
    (tmp_path / "dry-run.json").write_text(json.dumps({"deploy_stages_enabled": True}))
    runner = FakeRunner()
    _run(ops_assembly, tmp_path, ssm, runner=runner)
    assert "finplan-shared-financialplanning-tooling:SourceDryRunPassed=true" in runner.calls[0]


def test_published_role_names_feed_the_budget_action(ops_assembly: Path, tmp_path: Path, ssm: Any) -> None:
    ssm.put_parameter(Name="/finplan/beta/financialplanning/config/budget-enforced-role-names", Value="finplan-beta-financialplanning-ingestion-handler-role", Type="String")
    ssm.put_parameter(Name="/finplan/beta/financemodel/config/budget-enforced-role-names", Value='["finplan-beta-financemodel-job-execution-role"]', Type="String")
    assert bootstrap.published_enforced_role_names(ssm) == ["finplan-beta-financemodel-job-execution-role", "finplan-beta-financialplanning-ingestion-handler-role"]
    runner = FakeRunner()
    _run(ops_assembly, tmp_path, ssm, runner=runner)
    assert "finplan-shared-financialplanning-tooling:AdditionalEnforcedRoleNames=finplan-beta-financemodel-job-execution-role,finplan-beta-financialplanning-ingestion-handler-role" in runner.calls[0]
    ssm.put_parameter(Name="/finplan/gamma/financeagent/config/budget-enforced-role-names", Value="arn:aws:iam::<account-id>:role/x", Type="String")
    with pytest.raises(BootstrapStop, match="role names only"):
        bootstrap.published_enforced_role_names(ssm)


def test_bootstrap_assembly_contains_no_environment_stack(ops_assembly: Path, tmp_path: Path) -> None:
    out = bootstrap.bootstrap_assembly(ops_assembly, tmp_path / "b")
    names = sorted(p.name for p in out.iterdir())
    assert names == ["PipelineStore.metadata.json", "PipelineStore.template.json", "Tooling.assets.json", "Tooling.metadata.json", "Tooling.template.json", "manifest.json"]
    manifest = json.loads((out / "manifest.json").read_text())
    for art in manifest["artifacts"].values():
        assert all(d in manifest["artifacts"] for d in art.get("dependencies", []))


def test_bootstrap_assembly_needs_no_cdk_toolkit_roles(ops_assembly: Path, tmp_path: Path) -> None:
    # Regression: the store stack once carried the default cdk-hnb659fds role ARNs, so the first
    # bootstrap failed in an account without the CDKToolkit stack.
    out = bootstrap.bootstrap_assembly(ops_assembly, tmp_path / "b")
    manifest = json.loads((out / "manifest.json").read_text())
    stacks = [a for a in manifest["artifacts"].values() if a.get("type") == "aws:cloudformation:stack"]
    assert stacks
    for art in stacks:
        props = art.get("properties", {})
        for key in ("assumeRoleArn", "cloudFormationExecutionRoleArn", "lookupRole", "requiresBootstrapStackVersion"):
            assert not props.get(key), (art, key)
    assert "cdk-hnb659fds" not in json.dumps(manifest)
    # nothing may be staged in a CDK asset bucket: only the tooling template, in the pipeline store
    for path in out.glob("*.assets.json"):
        for asset in json.loads(path.read_text())["files"].values():
            for dest in asset["destinations"].values():
                assert dest["bucketName"].startswith("finplan-shared-financialplanning-pipeline-store-"), (path.name, dest)
    store = manifest["artifacts"]["PipelineStore"]["properties"]
    assert not store.get("stackTemplateAssetObjectUrl")


# ------------------------------------------------------------------ COST-06
def test_default_allocation_written_when_absent_COST_06(ssm: Any) -> None:
    res = bootstrap.ensure_budget_allocation(ssm, 50.0)
    assert res.written and res.allocation == {"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 25, "reserve": 5}
    stored = json.loads(ssm.get_parameter(Name=ALLOCATION_PARAMETER)["Parameter"]["Value"])
    assert stored == DEFAULT_ALLOCATION and sum(stored.values()) == 50


def test_user_allocation_preserved_COST_06(ssm: Any) -> None:
    user = {"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 20, "reserve": 10}
    ssm.put_parameter(Name=ALLOCATION_PARAMETER, Value=json.dumps(user), Type="String")
    res = bootstrap.ensure_budget_allocation(ssm, 50.0)
    assert not res.written and res.allocation == user
    assert json.loads(ssm.get_parameter(Name=ALLOCATION_PARAMETER)["Parameter"]["Value"]) == user


def test_allocation_above_the_ceiling_rejected_COST_06(ssm: Any) -> None:
    over = {"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 35, "reserve": 5}  # sums to 60
    ssm.put_parameter(Name=ALLOCATION_PARAMETER, Value=json.dumps(over), Type="String")
    with pytest.raises(BootstrapStop) as ei:
        bootstrap.ensure_budget_allocation(ssm, 50.0)
    assert ei.value.step == "budget-allocation" and "BUDGET_EXCEEDED" in ei.value.message
    # pre-flight checks that read this allocation refuse paid work (contract preflight)
    from finplan_contracts.budget import preflight

    assert not preflight("gpu", 1.0, over, ceiling_usd=50.0).allowed


def test_ceiling_written_once_and_never_overwritten_COST_01(ssm: Any) -> None:
    assert bootstrap.ensure_cost_ceiling(ssm, 50) == (50.0, True)
    ssm.put_parameter(Name=CEILING_PARAMETER, Value="75", Type="String", Overwrite=True)  # the user raised it
    assert bootstrap.ensure_cost_ceiling(ssm, 50) == (75.0, False)
    res = bootstrap.ensure_budget_allocation(ssm, 75.0)
    assert res.written and sum(res.allocation.values()) == 50


def test_scope_to_project_tag_is_passed_to_the_tooling_stack() -> None:
    # Regression: the first bootstrap left the budget on the whole account, whose prior spend
    # exceeded the ceiling, so the budget action denied the pipeline's first build.
    on = bootstrap.deploy_parameters(notification_email=None, enforced_roles=[], dry_run_passed=True, scope_to_project_tag=True)
    off = bootstrap.deploy_parameters(notification_email=None, enforced_roles=[], dry_run_passed=True)
    assert on[bootstrap.TOOLING_STACK_NAME]["ScopeBudgetToProjectTag"] == "true"
    assert off[bootstrap.TOOLING_STACK_NAME]["ScopeBudgetToProjectTag"] == "false"
