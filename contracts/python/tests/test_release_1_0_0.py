"""Contracts 1.0.0 (design D16): the FinanceModel ownership rows, the shared budget-enforced-role-names
key and the contract registry module (OWN-01, OWN-02, ENV-07, ENV-19, CS-04)."""

from __future__ import annotations

import json

import pytest
from finplan_contracts import iam, registry, ssm
from finplan_contracts.ownership import Matrix, check_template

#: The (type, logical-role) pairs the FinanceModel verification report asked for, by matrix row.
FINANCEMODEL_PAIRS = {
    "job-interface": [("AWS::ApiGateway::RestApi", "job-api"), ("AWS::ApiGateway::Deployment", "job-api"), ("AWS::ApiGateway::Stage", "job-api")],
    "sagemaker-job-definitions": [("AWS::IAM::Role", "job-execution-role")],
    "model-registry": [("AWS::S3::BucketPolicy", "model-registry")],
    "pipeline-financemodel": [("AWS::Logs::LogGroup", "pipeline-build-project")],
}
SUBST = {"AWS::Partition": "aws", "AWS::Region": "us-east-2", "AWS::AccountId": "<account-id>"}


@pytest.fixture(scope="module")
def matrix() -> Matrix:
    return Matrix.load()


def _tags(repo: str, env: str, role: str) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in ssm.cost_allocation_tags(repo, env, role).items()]


def _template(repo: str, row: str, rtype: str, role: str) -> dict:
    env = "shared" if row.startswith("pipeline-") else "beta"
    return {"Resources": {"R": {"Type": rtype, "Properties": {"Tags": _tags(repo, env, role)}}}}


@pytest.mark.parametrize("row, rtype, role", [(row, t, r) for row, pairs in FINANCEMODEL_PAIRS.items() for t, r in pairs])
def test_financemodel_rows_cover_its_job_api_job_role_registry_policy_and_pipeline_logs(matrix: Matrix, row: str, rtype: str, role: str) -> None:
    """OWN-01: FinanceModel may declare each pair; FinancialPlanning may not (single owner)."""
    report = check_template(_template("financemodel", row, rtype, role), "financemodel", matrix=matrix)
    assert report.ok, [str(p) for p in report.problems]
    assert report.matched == {"R": row}
    if not row.startswith("pipeline-"):
        assert not check_template(_template("financialplanning", row, rtype, role), "financialplanning", matrix=matrix).ok


def test_job_execution_role_logical_role_is_unique(matrix: Matrix) -> None:
    rows = [r["id"] for r in matrix.rows_for_role("job-execution-role")]
    assert rows == ["sagemaker-job-definitions"]


def test_contract_registry_row_lists_its_reference_parameter(matrix: Matrix) -> None:
    row = next(r for r in matrix.rows_for_role("contract-registry"))
    assert row["owner"] == "financialplanning" and row["scope"] == "shared"
    assert {"AWS::CodeArtifact::Domain", "AWS::CodeArtifact::Repository", "AWS::SSM::Parameter"} <= set(row["resource_types"])
    assert row["reference"] == registry.REGISTRY_REF_PARAMETER


# ------------------------------------------------------------------ shared budget-enforced-role-names (D4, D16)
@pytest.mark.parametrize("repo", ssm.REPOS)
def test_shared_budget_role_names_key_is_registered_and_bootstrap_written(repo: str) -> None:
    path = f"/finplan/shared/{repo}/config/budget-enforced-role-names"
    reg = ssm.find_registered(path)
    assert reg is not None and reg.key == "shared-budget-enforced-role-names" and reg.writers == ("bootstrap",)
    assert ssm.check_write(path, ssm.Writer(repo, "bootstrap"))
    assert not ssm.check_write(path, ssm.Writer(repo, "pipeline"))  # shared: never a deploy pipeline
    other = next(r for r in ssm.REPOS if r != repo)
    assert not ssm.check_write(path, ssm.Writer(other, "bootstrap"))  # cross-repo
    assert ssm.validate_value(path, f"finplan-shared-{repo}-pipeline-role,finplan-shared-{repo}-pipeline-build-project-role") == []
    assert ssm.validate_value(path, "arn:aws:iam::<account-id>:role/x")


def test_budget_action_reads_shared_and_per_environment_keys() -> None:
    keys = ssm.budget_enforced_role_name_keys()
    assert len(keys) == len(ssm.REPOS) * 4
    assert "/finplan/shared/financemodel/config/budget-enforced-role-names" in keys
    assert "/finplan/prod/financeagent/config/budget-enforced-role-names" in keys
    assert all(ssm.find_registered(k) is not None for k in keys)


def test_budget_template_takes_the_shared_lists() -> None:
    from finplan_contracts.budget import budget_template

    params = budget_template()["Parameters"]
    defaults = {p.get("Default") for p in params.values()}
    assert set(ssm.budget_enforced_role_name_keys()) <= defaults


def test_budget_state_writer_may_read_the_shared_lists() -> None:
    doc = iam.substitute(iam.budget_state_writer_policy(), SUBST)
    arn = "arn:aws:ssm:us-east-2:<account-id>:parameter/finplan/shared/financemodel/config/budget-enforced-role-names"
    assert iam.is_allowed("ssm:GetParameter", arn, [doc])


# ------------------------------------------------------------------ contract registry (D3, D16)
def test_registry_reference_round_trip_holds_no_account_or_endpoint() -> None:
    value = json.dumps(registry.registry_ref_value("us-east-2"))
    doc = registry.parse_registry_ref(value)
    assert doc == {"domain": "finplan", "repository": "contracts", "region": "us-east-2", "formats": ["pypi", "npm"]}
    assert ssm.find_registered(registry.REGISTRY_REF_PARAMETER).key == "contract-registry-ref"
    for bad in ("not json", json.dumps([]), json.dumps({**doc, "domain_owner": "<account-id>"}), json.dumps({**doc, "formats": ["maven"]}), json.dumps({**doc, "domain": "Bad Name"})):
        with pytest.raises(ValueError):
            registry.parse_registry_ref(bad)


def _arn(kind: str) -> str:
    return iam.substitute({"domain": registry.domain_arn(), "repository": registry.repository_arn(), "wheel": registry.package_arn("pypi", "", registry.PYPI_PACKAGE), "npm": registry.package_arn("npm", registry.NPM_NAMESPACE, registry.NPM_PACKAGE)}[kind], SUBST)


def test_read_policy_installs_but_never_publishes() -> None:
    doc = iam.substitute(registry.read_policy(), SUBST)
    assert iam.is_allowed("codeartifact:GetAuthorizationToken", _arn("domain"), [doc])
    assert iam.is_allowed("codeartifact:ReadFromRepository", _arn("repository"), [doc])
    assert iam.is_allowed("sts:GetServiceBearerToken", "*", [doc], **{"sts:AWSServiceName": "codeartifact.amazonaws.com"})
    assert not iam.is_allowed("sts:GetServiceBearerToken", "*", [doc], **{"sts:AWSServiceName": "lambda.amazonaws.com"})
    assert not iam.is_allowed("codeartifact:PublishPackageVersion", _arn("wheel"), [doc])
    other = _arn("repository").replace("/contracts", "/other")
    assert not iam.is_allowed("codeartifact:ReadFromRepository", other, [doc])


def test_publish_policy_is_scoped_to_the_contract_repository_and_never_deletes() -> None:
    doc = iam.substitute(registry.publish_policy(), SUBST)
    for kind in ("wheel", "npm"):
        assert iam.is_allowed("codeartifact:PublishPackageVersion", _arn(kind), [doc])
        assert iam.is_allowed("codeartifact:DescribePackageVersion", _arn(kind), [doc])
    for action in ("codeartifact:DeletePackageVersions", "codeartifact:DisposePackageVersions", "codeartifact:UpdatePackageVersionsStatus", "codeartifact:DeleteRepository", "codeartifact:PutRepositoryPermissionsPolicy"):
        assert not iam.is_allowed(action, _arn("wheel"), [doc]) and not iam.is_allowed(action, _arn("repository"), [doc])
    foreign = _arn("wheel").replace("/finplan/contracts/", "/finplan/other/")
    assert not iam.is_allowed("codeartifact:PublishPackageVersion", foreign, [doc])


def test_resource_policies_admit_only_the_other_repositories_build_roles() -> None:
    patterns = registry.reader_role_patterns(partition="aws", account="<account-id>")
    assert patterns == [f"arn:aws:iam::<account-id>:role/finplan-shared-{r}-*" for r in ("financemodel", "financelambdastool", "financeagent")]
    for doc, actions in ((registry.domain_policy(), ["codeartifact:GetAuthorizationToken"]), (registry.repository_policy(), list(registry.READ_ACTIONS))):
        (st,) = doc["Statement"]
        assert st["Effect"] == "Allow" and st["Action"] == actions
        assert st["Condition"]["StringEquals"] == {"aws:PrincipalAccount": "${AWS::AccountId}"}
        assert not any("Publish" in a or "Delete" in a for a in st["Action"])
