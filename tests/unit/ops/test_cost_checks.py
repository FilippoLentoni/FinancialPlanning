"""Build-stage cost checks (task 9.3; COST-03 cost-allocation tags, COST-04 no always-on compute).

Fixture templates are minimal synthetic CloudFormation documents; the real synthesized assembly
must pass both checks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import cost_checks

TAGS = [{"Key": "project", "Value": "finplan"}, {"Key": "owner-repo", "Value": "financialplanning"}, {"Key": "environment", "Value": "beta"}, {"Key": "logical-role", "Value": "raw-input-bucket"}]


def _tpl(**resources: dict[str, Any]) -> dict[str, Any]:
    return {"Resources": resources}


def _rules(findings: list[cost_checks.Finding]) -> list[tuple[str, str]]:
    return [(f.rule, f.logical_id) for f in findings]


# ------------------------------------------------------------------ COST-03
def test_tagged_resources_pass_COST_03() -> None:
    t = _tpl(Raw={"Type": "AWS::S3::Bucket", "Properties": {"Tags": TAGS}}, Pol={"Type": "AWS::S3::BucketPolicy", "Properties": {"Bucket": {"Ref": "Raw"}}})
    assert cost_checks.check_template(t) == []


def test_untagged_resource_fails_and_is_named_COST_03() -> None:
    t = _tpl(Untagged={"Type": "AWS::DynamoDB::Table", "Properties": {"BillingMode": "PAY_PER_REQUEST"}})
    findings = cost_checks.check_template(t, "fixture.template.json")
    assert _rules(findings) == [("COST-03", "Untagged")]
    assert "Untagged" in str(findings[0]) and "environment" in findings[0].message


def test_missing_environment_tag_fails_COST_03() -> None:
    tags = [t for t in TAGS if t["Key"] != "environment"]
    t = _tpl(Fn={"Type": "AWS::Lambda::Function", "Properties": {"Tags": tags}})
    (f,) = cost_checks.check_template(t)
    assert f.rule == "COST-03" and "environment" in f.message and "project" not in f.message


def test_tag_maps_and_budget_resource_tags_are_read_COST_03() -> None:
    as_map = {t["Key"]: t["Value"] for t in TAGS}
    t = _tpl(P={"Type": "AWS::SSM::Parameter", "Properties": {"Tags": as_map}}, B={"Type": "AWS::Budgets::Budget", "Properties": {"ResourceTags": TAGS}})
    assert cost_checks.check_template(t) == []
    t = _tpl(B={"Type": "AWS::Budgets::Budget", "Properties": {"ResourceTags": TAGS[:1]}})
    assert _rules(cost_checks.check_template(t)) == [("COST-03", "B")]


def test_untaggable_types_are_not_required_to_carry_tags_COST_03() -> None:
    t = _tpl(M={"Type": "AWS::ApiGateway::Method", "Properties": {}}, S={"Type": "AWS::Scheduler::Schedule", "Properties": {}}, A={"Type": "AWS::KMS::Alias", "Properties": {}})
    assert cost_checks.check_template(t) == []


# ------------------------------------------------------------------ COST-04
@pytest.mark.parametrize(
    ("rtype", "props"),
    [
        ("AWS::EC2::Instance", {"InstanceType": "t3.micro"}),
        ("AWS::EC2::NatGateway", {}),
        ("AWS::EC2::VPCEndpoint", {"VpcEndpointType": "Interface"}),
        ("AWS::SageMaker::Endpoint", {}),
        ("AWS::RDS::DBInstance", {}),
        ("AWS::ECS::Service", {}),
        ("AWS::ElasticLoadBalancingV2::LoadBalancer", {}),
        ("AWS::DynamoDB::Table", {"BillingMode": "PROVISIONED", "ProvisionedThroughput": {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}}),
        ("AWS::DynamoDB::Table", {}),  # default billing mode is provisioned
        ("AWS::Lambda::Alias", {"ProvisionedConcurrencyConfig": {"ProvisionedConcurrentExecutions": 1}}),
        ("AWS::ApiGateway::Stage", {"CacheClusterEnabled": True}),
        ("AWS::SageMaker::EndpointConfig", {"ProductionVariants": [{"InstanceType": "ml.g6.12xlarge"}]}),
    ],
)
def test_always_on_or_provisioned_resources_fail_COST_04(rtype: str, props: dict[str, Any]) -> None:
    t = _tpl(Bad={"Type": rtype, "Properties": props})
    findings = [f for f in cost_checks.check_template(t) if f.rule == "COST-04"]
    assert findings and all(f.logical_id == "Bad" for f in findings)


def test_gpu_instance_type_anywhere_fails_COST_04() -> None:
    t = _tpl(Job={"Type": "AWS::Batch::ComputeEnvironment", "Properties": {"ComputeResources": {"InstanceTypes": ["c6g.large"], "LaunchTemplate": {"InstanceType": "p4d.24xlarge"}}}})
    findings = cost_checks.always_on_findings(t)
    assert [f.rule for f in findings] == ["COST-04"] and "p4d.24xlarge" in findings[0].message


def test_serverless_resources_pass_COST_04() -> None:
    t = _tpl(
        Table={"Type": "AWS::DynamoDB::Table", "Properties": {"BillingMode": "PAY_PER_REQUEST"}},
        Gw={"Type": "AWS::EC2::VPCEndpoint", "Properties": {"VpcEndpointType": "Gateway"}},
        Fn={"Type": "AWS::Lambda::Function", "Properties": {"Architectures": ["arm64"]}},
        Stage={"Type": "AWS::ApiGateway::Stage", "Properties": {"CacheClusterEnabled": False}},
    )
    assert cost_checks.always_on_findings(t) == []


# ------------------------------------------------------------------ real assembly and CLI
def test_synthesized_assembly_passes_both_checks(ops_assembly: Path) -> None:
    n, findings = cost_checks.check_paths([ops_assembly])
    assert n >= 14 and findings == [], [str(f) for f in findings]


def test_cli_fails_on_a_fixture_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "Bad.template.json"
    bad.write_text(json.dumps(_tpl(Nat={"Type": "AWS::EC2::NatGateway", "Properties": {"Tags": TAGS}})))
    assert cost_checks.main([str(tmp_path)]) == 1
    assert "Nat" in capsys.readouterr().err
    good = tmp_path / "good"
    good.mkdir()
    (good / "Good.template.json").write_text(json.dumps(_tpl(T={"Type": "AWS::DynamoDB::Table", "Properties": {"BillingMode": "PAY_PER_REQUEST", "Tags": TAGS}})))
    assert cost_checks.main([str(good)]) == 0
    assert cost_checks.main([str(tmp_path / "empty-dir-does-not-exist")]) == 1
