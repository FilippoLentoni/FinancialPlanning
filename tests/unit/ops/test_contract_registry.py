"""Contract registry in the tooling stack and the build-stage publish step (contracts task 7.3; design P10, D3, D16).

* the tooling template declares the CodeArtifact domain ``finplan``, the repository ``contracts``
  (both retained) and ``/finplan/shared/financialplanning/contract/registry-ref``, attributed to the
  FinancialPlanning ``contract-registry`` row;
* the build role may publish to that one repository and never delete; the other repositories' build
  roles (``finplan-shared-<repo>-*``) may read through the resource policies; nobody else may;
* the build spec publishes only after ``scripts/build_stage.py`` (every gate) succeeded.

Offline: the policies are simulated with :mod:`infra.policy_sim`; no AWS call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from finplan_contracts import registry as contract_registry
from finplan_contracts.boundaries import shared_permission_boundary
from finplan_contracts.ownership import check_template

from infra.policy_sim import Principal, simulate
from infra.stacks.pipeline import build_spec
from tests.unit.ops.conftest import resources_of, tags_of

ACCOUNT = "<account-id>"
DOMAIN_ARN = f"arn:aws:codeartifact:us-east-2:{ACCOUNT}:domain/finplan"
REPO_ARN = f"arn:aws:codeartifact:us-east-2:{ACCOUNT}:repository/finplan/contracts"
WHEEL_ARN = f"arn:aws:codeartifact:us-east-2:{ACCOUNT}:package/finplan/contracts/pypi//finplan-contracts"
NPM_ARN = f"arn:aws:codeartifact:us-east-2:{ACCOUNT}:package/finplan/contracts/npm/finplan/contracts"
REF_ARN = f"arn:aws:ssm:us-east-2:{ACCOUNT}:parameter/finplan/shared/financialplanning/contract/registry-ref"
BUILD_ROLE = "finplan-shared-financialplanning-pipeline-build-project-role"


def _one(template: dict[str, Any], rtype: str) -> dict[str, Any]:
    (res,) = resources_of(template, rtype).values()
    return res


def test_registry_resources_are_declared_and_retained(tooling_template: dict[str, Any]) -> None:
    domain = _one(tooling_template, "AWS::CodeArtifact::Domain")
    repo = _one(tooling_template, "AWS::CodeArtifact::Repository")
    assert domain["Properties"]["DomainName"] == "finplan"
    assert repo["Properties"]["RepositoryName"] == "contracts"
    assert "Upstreams" not in repo["Properties"] and "ExternalConnections" not in repo["Properties"]  # finplan packages only
    for res, role in ((domain, "contract-registry-domain"), (repo, "contract-registry")):
        assert res["DeletionPolicy"] == res["UpdateReplacePolicy"] == "Retain"
        assert tags_of(res) == {"project": "finplan", "owner-repo": "financialplanning", "environment": "shared", "logical-role": role}
    params = [r for r in resources_of(tooling_template, "AWS::SSM::Parameter").values() if r["Properties"]["Name"] == contract_registry.REGISTRY_REF_PARAMETER]
    assert len(params) == 1 and tags_of(params[0])["logical-role"] == "contract-registry"


def test_registry_reference_value_parses(tooling_template: dict[str, Any]) -> None:
    (param,) = [r for r in resources_of(tooling_template, "AWS::SSM::Parameter").values() if r["Properties"]["Name"] == contract_registry.REGISTRY_REF_PARAMETER]
    def part(p: Any) -> str:
        if isinstance(p, str):
            return p
        if p.get("Ref") == "AWS::Region":
            return "us-east-2"
        lid, attr = p["Fn::GetAtt"]  # the name of the domain or repository resource
        props = tooling_template["Resources"][lid]["Properties"]
        assert attr == "Name"
        return str(props.get("DomainName") if "RepositoryName" not in props else props["RepositoryName"])

    rendered = "".join(part(p) for p in param["Properties"]["Value"]["Fn::Join"][1])
    assert contract_registry.parse_registry_ref(rendered) == {"domain": "finplan", "repository": "contracts", "region": "us-east-2", "formats": ["pypi", "npm"]}


def test_registry_resources_pass_the_ownership_check(tooling_template: dict[str, Any]) -> None:
    report = check_template(tooling_template, "financialplanning")
    assert report.ok, [str(p) for p in report.problems]
    registry_rows = {lid: row for lid, row in report.matched.items() if row == "contract-registry"}
    assert len(registry_rows) == 3


def _build_role(resolver: Any) -> Principal:
    return Principal.role(BUILD_ROLE, *resolver.role_policies(BUILD_ROLE), boundary=shared_permission_boundary())


def test_build_role_publishes_to_the_contract_repository_only(tooling_resolver: Any) -> None:
    build = _build_role(tooling_resolver)
    assert simulate("codeartifact:GetAuthorizationToken", DOMAIN_ARN, build).allowed
    assert simulate("sts:GetServiceBearerToken", "*", build, context={"sts:AWSServiceName": "codeartifact.amazonaws.com"}).allowed
    assert not simulate("sts:GetServiceBearerToken", "*", build, context={"sts:AWSServiceName": "lambda.amazonaws.com"}).allowed
    assert simulate("ssm:GetParameter", REF_ARN, build).allowed
    for arn in (WHEEL_ARN, NPM_ARN):
        for action in ("codeartifact:PublishPackageVersion", "codeartifact:DescribePackageVersion", "codeartifact:ListPackageVersionAssets", "codeartifact:GetPackageVersionAsset"):
            assert simulate(action, arn, build).allowed, (action, arn)
        for action in ("codeartifact:DeletePackageVersions", "codeartifact:DisposePackageVersions", "codeartifact:UpdatePackageVersionsStatus", "codeartifact:CopyPackageVersions"):
            assert not simulate(action, arn, build).allowed, (action, arn)
    assert simulate("codeartifact:ReadFromRepository", REPO_ARN, build).allowed
    for action in ("codeartifact:DeleteRepository", "codeartifact:PutRepositoryPermissionsPolicy", "codeartifact:AssociateExternalConnection", "codeartifact:UpdateRepository"):
        assert not simulate(action, REPO_ARN, build).allowed, action
    assert not simulate("codeartifact:PublishPackageVersion", WHEEL_ARN.replace("/contracts/", "/other/"), build).allowed
    assert not simulate("codeartifact:DeleteDomain", DOMAIN_ARN, build).allowed


@pytest.mark.parametrize("repo", ["financemodel", "financelambdastool", "financeagent"])
def test_other_repositories_build_roles_read_through_the_resource_policies(tooling_template: dict[str, Any], tooling_resolver: Any, repo: str) -> None:
    domain_policy = tooling_resolver.resolve(_one(tooling_template, "AWS::CodeArtifact::Domain")["Properties"]["PermissionsPolicyDocument"], "tooling")
    repo_policy = tooling_resolver.resolve(_one(tooling_template, "AWS::CodeArtifact::Repository")["Properties"]["PermissionsPolicyDocument"], "tooling")
    reader = Principal.role(f"finplan-shared-{repo}-pipeline-build-project-role")  # no identity grant: the resource policy alone
    ctx = {"aws:PrincipalAccount": ACCOUNT}
    assert simulate("codeartifact:GetAuthorizationToken", DOMAIN_ARN, reader, resource_policy=domain_policy, context=ctx).allowed
    assert simulate("codeartifact:ReadFromRepository", REPO_ARN, reader, resource_policy=repo_policy, context=ctx).allowed
    assert simulate("codeartifact:GetRepositoryEndpoint", REPO_ARN, reader, resource_policy=repo_policy, context=ctx).allowed
    assert not simulate("codeartifact:PublishPackageVersion", WHEEL_ARN, reader, resource_policy=repo_policy, context=ctx).allowed
    for outsider in (f"finplan-beta-{repo}-job-role", "some-other-role", "finplan-shared-website-role"):
        p = Principal.role(outsider)
        assert not simulate("codeartifact:ReadFromRepository", REPO_ARN, p, resource_policy=repo_policy, context=ctx).allowed, outsider
    foreign = Principal(f"arn:aws:iam::<other-account>:role/finplan-shared-{repo}-pipeline-build-project-role")
    assert not simulate("codeartifact:ReadFromRepository", REPO_ARN, foreign, resource_policy=repo_policy, context={"aws:PrincipalAccount": "<other-account>"}).allowed


def test_build_spec_publishes_after_the_gated_build(tooling_template: dict[str, Any]) -> None:
    commands = build_spec()["phases"]["build"]["commands"]
    build = next(i for i, c in enumerate(commands) if "scripts/build_stage.py" in c)
    publish = next(i for i, c in enumerate(commands) if "scripts/publish_contracts.py" in c)
    assert publish == build + 1 == len(commands) - 1
    assert '--rollback-to "$ROLLBACK_TO_RELEASE_ID"' in commands[publish]
    assert "nodejs" in build_spec()["phases"]["install"]["runtime-versions"]  # npm for conformance-ts and the npm package
    (project,) = [r for r in resources_of(tooling_template, "AWS::CodeBuild::Project").values() if "scripts/build_stage.py" in json.dumps(r["Properties"]["Source"])]
    assert "scripts/publish_contracts.py" in json.dumps(project["Properties"]["Source"])
