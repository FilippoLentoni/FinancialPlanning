"""ENV-12 / ENV-13 / task 14.4: bootstrap pre-run plan, pre-checks and source-stage dry run, all with
mocked boto3-style clients (no AWS call, no mutation)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest

from finplan_contracts import bootstrap
from finplan_contracts.bootstrap import (
    EXTEND_INSTALLATION_MESSAGE,
    ROOT_RECOMMENDATION,
    BootstrapConfig,
    BootstrapStop,
    Clients,
    check_deploy_roles,
    load_config,
    prerun,
    run_bootstrap,
)

from infra_helpers import INFRA, infra

ACCOUNT = "<account-id>"
CONN = "arn:aws:codeconnections:us-east-2:<account-id>:connection/synthetic-connection"
ASSEMBLY = INFRA / "assembly"


def cfg(**kw):
    base = dict(account_id=ACCOUNT, primary_region="us-east-2", repo="financialplanning", codeconnection_arn=CONN, github_repository="FilippoLentoni/FinancialPlanning")
    base.update(kw)
    return BootstrapConfig.from_mapping(base)


# ------------------------------------------------------------------ mocks
class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def record(self, _call, **kw):
        self.calls.append((_call, kw))

    def names(self):
        return [c[0] for c in self.calls]


class FakeSts(Recorder):
    def __init__(self, account=ACCOUNT, arn=f"arn:aws:iam::{ACCOUNT}:root"):
        super().__init__()
        self.ident = {"Account": account, "Arn": arn, "UserId": "synthetic"}

    def get_caller_identity(self):
        self.record("get_caller_identity")
        return self.ident


class FakeConnections(Recorder):
    def __init__(self, status="AVAILABLE"):
        super().__init__()
        self.status = status

    def get_connection(self, ConnectionArn):
        self.record("get_connection", ConnectionArn=ConnectionArn)
        return {"Connection": {"ConnectionArn": ConnectionArn, "ConnectionStatus": self.status, "ProviderType": "GitHub"}}


class FakeSsm(Recorder):
    def put_parameter(self, **kw):
        self.record("put_parameter", **kw)
        return {"Version": 1}


class FakePipeline(Recorder):
    def __init__(self, statuses=("InProgress", "Succeeded")):
        super().__init__()
        self.statuses = list(statuses)

    def disable_stage_transition(self, **kw):
        self.record("disable_stage_transition", **kw)

    def enable_stage_transition(self, **kw):
        self.record("enable_stage_transition", **kw)

    def start_pipeline_execution(self, **kw):
        self.record("start_pipeline_execution", **kw)
        return {"pipelineExecutionId": "synthetic-execution-1"}

    def get_pipeline_state(self, **kw):
        self.record("get_pipeline_state", **kw)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"stageStates": [{"stageName": "Source", "latestExecution": {"pipelineExecutionId": "synthetic-execution-1", "status": status}}, {"stageName": "Build", "inboundTransitionState": {"enabled": False}}]}


def _price_list(usd):
    return [json.dumps({"product": {"sku": "SYNTH"}, "terms": {"OnDemand": {"T": {"priceDimensions": {"D": {"unit": unit, "pricePerUnit": {"USD": str(usd)}}}}}}}) for unit in ("minute", "GB-Mo", "Keys", "Requests", "Budget-Day")]


class FakePricing(Recorder):
    def __init__(self, usd=0.01):
        super().__init__()
        self.usd = usd

    def get_products(self, **kw):
        self.record("get_products", **kw)
        return {"PriceList": _price_list(self.usd), "FormatVersion": "aws_v1"}


def clients(**kw):
    c = dict(sts=FakeSts(), codeconnections=FakeConnections(), ssm=FakeSsm(), codepipeline=FakePipeline(), pricing=FakePricing())
    c.update(kw)
    return Clients(**c)


FIXED_NOW = lambda: datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)  # noqa: E731


# ------------------------------------------------------------------ 14.4 pre-run plan
def test_prerun_refuses_without_a_synthesized_assembly(tmp_path):
    with pytest.raises(BootstrapStop) as e:
        prerun(tmp_path / "cdk.out", FakePricing(), region="us-east-2")
    assert e.value.step == "prerun" and "not synthesized" in e.value.message


def test_prerun_prints_exact_stacks_and_estimate_from_the_pricing_client():
    out: list[str] = []
    pricing = FakePricing(0.01)
    plan = prerun(ASSEMBLY, pricing, region="us-east-2", now=FIXED_NOW, out=out.append)
    assert plan.stack_names == ["finplan-shared-financialplanning-pipeline", "finplan-shared-financialplanning-tooling"]
    text = "\n".join(out)
    for name in plan.stack_names:
        assert name in text
    assert "price" in text.lower() and "2026-10-07T12:00:00Z" in text
    assert plan.monthly_estimate_usd > 0
    assert pricing.calls and all(c[1]["Filters"] for c in pricing.calls)
    assert "AWS::IAM::Role" in plan.not_billed


def test_estimate_has_no_hard_coded_prices():
    a = prerun(ASSEMBLY, FakePricing(0.01), region="us-east-2", out=lambda s: None)
    b = prerun(ASSEMBLY, FakePricing(0.02), region="us-east-2", out=lambda s: None)
    assert b.monthly_estimate_usd == pytest.approx(2 * a.monthly_estimate_usd)

    class NoPrices(FakePricing):
        def get_products(self, **kw):
            return {"PriceList": []}

    c = prerun(ASSEMBLY, NoPrices(), region="us-east-2", out=lambda s: None)
    assert c.monthly_estimate_usd == 0 and c.estimate_lines == [] and c.not_estimated


# ------------------------------------------------------------------ ENV-12
def test_root_caller_proceeds_with_recommendation():
    out: list[str] = []
    cl = clients()
    deployed: list[list[str]] = []
    report = run_bootstrap(cfg(), cl, session_region="us-east-2", assembly_dir=ASSEMBLY, approve=lambda plan: True, deployer=deployed.append, out=out.append, sleep=lambda s: None)
    assert report.completed and report.caller_is_root
    assert ROOT_RECOMMENDATION in out
    assert deployed == [report.plan.stack_names]
    put = [c for c in cl.ssm.calls if c[0] == "put_parameter"]
    assert len(put) == 1 and put[0][1]["Name"] == "/finplan/shared/financialplanning/config/codeconnection-ref"


def test_bootstrap_under_in_principle_approval_shows_stacks_and_estimate_first():
    order: list[str] = []
    out: list[str] = []

    def approve(plan):
        order.append("approve")
        assert plan.stack_names and plan.monthly_estimate_usd >= 0
        assert any("Stacks to deploy" in line for line in out) and any("Cost estimate" in line for line in out)
        return True

    run_bootstrap(cfg(), clients(), session_region="us-east-2", assembly_dir=ASSEMBLY, approve=approve, deployer=lambda s: order.append("deploy"), out=out.append, sleep=lambda s: None)
    assert order == ["approve", "deploy"]


def test_bootstrap_does_not_run_before_the_iac_exists(tmp_path):
    cl = clients()
    deployer_calls = []
    with pytest.raises(BootstrapStop) as e:
        run_bootstrap(cfg(), cl, session_region="us-east-2", assembly_dir=tmp_path / "missing", approve=lambda p: True, deployer=deployer_calls.append, out=lambda s: None)
    assert e.value.step == "prerun" and deployer_calls == [] and cl.ssm.calls == [] and cl.sts.calls == []


def test_account_mismatch_stops_before_creating_anything():
    cl = clients(sts=FakeSts(account="<other-account-id>"))
    deployer_calls = []
    with pytest.raises(BootstrapStop) as e:
        run_bootstrap(cfg(), cl, session_region="us-east-2", assembly_dir=ASSEMBLY, approve=lambda p: True, deployer=deployer_calls.append, out=lambda s: None)
    assert e.value.step == "caller"
    assert deployer_calls == [] and cl.ssm.calls == [] and cl.codepipeline.calls == [] and cl.codeconnections.calls == []


def test_region_mismatch_stops():
    cl = clients()
    with pytest.raises(BootstrapStop) as e:
        run_bootstrap(cfg(), cl, session_region="us-west-2", assembly_dir=ASSEMBLY, approve=lambda p: True, deployer=lambda s: None, out=lambda s: None)
    assert e.value.step == "region" and cl.ssm.calls == []


def test_declined_approval_deploys_nothing():
    cl = clients()
    with pytest.raises(BootstrapStop) as e:
        run_bootstrap(cfg(), cl, session_region="us-east-2", assembly_dir=ASSEMBLY, approve=lambda p: False, deployer=lambda s: pytest.fail("deployed"), out=lambda s: None)
    assert e.value.step == "approval" and cl.ssm.calls == []


def test_deploy_actions_use_scoped_roles():
    caller = f"arn:aws:iam::{ACCOUNT}:root"
    assert check_deploy_roles(infra("pipelines/valid/standard.json"), caller) == []
    assert any("caller" in p for p in check_deploy_roles(infra("pipelines/invalid/deploy-as-caller.json"), caller))
    assert any("no scoped deploy role" in p for p in check_deploy_roles(infra("pipelines/invalid/prod-deploy-without-scoped-role.json"), caller))
    tpl = infra("pipelines/valid/standard.json")
    tpl["Resources"]["Pipeline"]["Properties"]["Stages"][2]["Actions"][0]["RoleArn"] = {"Fn::GetAtt": ["PipelineRole", "Arn"]}
    assert any("deploy-role" in p for p in check_deploy_roles(tpl, caller))


def test_unscoped_deploy_role_in_assembly_stops_bootstrap(tmp_path):
    import shutil

    asm = tmp_path / "cdk.out"
    shutil.copytree(ASSEMBLY, asm)
    (asm / "FinplanPipelineStack.template.json").write_text(json.dumps(infra("pipelines/invalid/deploy-as-caller.json")))
    with pytest.raises(BootstrapStop) as e:
        run_bootstrap(cfg(), clients(), session_region="us-east-2", assembly_dir=asm, approve=lambda p: True, deployer=lambda s: pytest.fail("deployed"), out=lambda s: None)
    assert e.value.step == "scoped-roles"


# ------------------------------------------------------------------ ENV-13
def test_connection_not_available_stops():
    cl = clients(codeconnections=FakeConnections("PENDING"))
    with pytest.raises(BootstrapStop) as e:
        run_bootstrap(cfg(), cl, session_region="us-east-2", assembly_dir=ASSEMBLY, approve=lambda p: True, deployer=lambda s: pytest.fail("deployed"), out=lambda s: None)
    assert e.value.step == "connection" and cl.ssm.calls == []


def test_dry_run_success_enables_remaining_stages_and_records_result(tmp_path):
    cl = clients()
    record = tmp_path / "dry-run.json"
    report = run_bootstrap(cfg(), cl, session_region="us-east-2", assembly_dir=ASSEMBLY, approve=lambda p: True, deployer=lambda s: None, out=lambda s: None, sleep=lambda s: None, record_path=record)
    names = cl.codepipeline.names()
    assert names[0] == "disable_stage_transition" and names[1] == "start_pipeline_execution" and names[-1] == "enable_stage_transition"
    assert cl.codepipeline.calls[-1][1] == {"pipelineName": "finplan-shared-financialplanning-pipeline", "stageName": "Build", "transitionType": "Inbound"}
    assert report.dry_run["status"] == "Succeeded" and report.dry_run["deploy_stages_enabled"] is True
    assert json.loads(record.read_text())["execution_id"] == "synthetic-execution-1"


def test_dry_run_failure_stops_with_extend_installation_message():
    cl = clients(codepipeline=FakePipeline(("InProgress", "Failed")))
    with pytest.raises(BootstrapStop) as e:
        run_bootstrap(cfg(github_repository="FilippoLentoni/FinanceModel"), cl, session_region="us-east-2", assembly_dir=ASSEMBLY, approve=lambda p: True, deployer=lambda s: None, out=lambda s: None, sleep=lambda s: None)
    assert e.value.step == "source-dry-run"
    assert e.value.message == EXTEND_INSTALLATION_MESSAGE.format(repository="FilippoLentoni/FinanceModel")
    assert "extend the github app installation" in e.value.message.lower() and "rerun the dry run" in e.value.message
    assert "enable_stage_transition" not in cl.codepipeline.names()


def test_dry_run_timeout_stops():
    cl = clients(codepipeline=FakePipeline(("InProgress",)))
    with pytest.raises(BootstrapStop):
        bootstrap.source_dry_run(cl.codepipeline, cfg(), sleep=lambda s: None, poll_seconds=1, timeout_seconds=3)
    assert "enable_stage_transition" not in cl.codepipeline.names()


# ------------------------------------------------------------------ configuration
def test_config_from_untracked_file_and_env_overrides(tmp_path):
    f = tmp_path / "bootstrap.json"
    f.write_text(json.dumps({"account_id": ACCOUNT, "primary_region": "us-east-2", "codeconnection_arn": CONN, "repo": "financemodel"}))
    c = load_config(f, environ={}, repo_root=tmp_path / "repo")
    assert c.repo == "financemodel" and c.pipeline_name == "finplan-shared-financemodel-pipeline"
    c2 = load_config(None, environ={"FINPLAN_BOOTSTRAP_CONFIG": str(f), "FINPLAN_PRIMARY_REGION": "us-west-2"}, repo_root=tmp_path / "repo")
    assert c2.primary_region == "us-west-2"
    c3 = load_config(tmp_path / "absent.json", environ={"FINPLAN_ACCOUNT_ID": ACCOUNT, "FINPLAN_PRIMARY_REGION": "us-east-2", "FINPLAN_CODECONNECTION_ARN": CONN}, repo_root=tmp_path / "repo")
    assert c3.account_id == ACCOUNT


def test_config_inside_the_repository_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "bootstrap.json"
    f.write_text(json.dumps({"account_id": ACCOUNT, "primary_region": "us-east-2", "codeconnection_arn": CONN}))
    with pytest.raises(ValueError, match="inside the repository"):
        load_config(f, environ={}, repo_root=repo)
    with pytest.raises(ValueError, match="missing"):
        load_config(tmp_path / "absent.json", environ={}, repo_root=repo)


def test_cli_check_and_prerun_with_injected_clients(tmp_path, capsys, monkeypatch):
    f = tmp_path / "bootstrap.json"
    f.write_text(json.dumps({"account_id": ACCOUNT, "primary_region": "us-east-2", "codeconnection_arn": CONN}))
    assert bootstrap.main(["--config", str(f), "check"], clients=clients(), session_region="us-east-2") == 0
    assert ROOT_RECOMMENDATION in capsys.readouterr().out
    assert bootstrap.main(["--config", str(f), "check"], clients=clients(sts=FakeSts(account="<other-account-id>")), session_region="us-east-2") == 1
    assert bootstrap.main(["--config", str(f), "prerun", "--assembly", str(tmp_path / "none")], clients=clients(), session_region="us-east-2") == 1
    assert bootstrap.main(["--config", str(f), "prerun", "--assembly", str(ASSEMBLY)], clients=clients(), session_region="us-east-2") == 0


def test_boto3_is_never_imported_by_the_tests():
    assert "boto3" not in sys.modules
