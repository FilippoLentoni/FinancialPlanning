"""OWN-01 (ownership check of synthesized templates), OWN-02 lookups through the check's matrix API, ENV-16 (template side), OWN-09 (CDK-generated helpers, task 1.5)."""

import json
from pathlib import Path

import pytest

from finplan_contracts import ownership
from finplan_contracts.ownership import Matrix, check_file, check_template

TEMPLATES = Path(__file__).parent / "fixtures" / "templates"


@pytest.fixture(scope="module")
def matrix():
    return Matrix.load()


def problems(report, rule="OWN-01"):
    return [p for p in report.problems if p.rule == rule]


def test_owned_and_mapped_template_passes(matrix):
    report = check_file(TEMPLATES / "financialplanning-beta-ok.json", "financialplanning", matrix)
    assert report.ok, [str(p) for p in report.problems]
    assert report.matched == {
        "DailyIngestSchedule": "daily-scheduler",
        "IngestionHandler": "ingestion-service",
        "PlanTable": "platform-metadata-tables",
        "PlatformKey": "platform-kms-keys",
        "PlatformKeyAlias": "platform-kms-keys",
        "RawInputBucket": "platform-input-buckets",
        "RawInputBucketPolicy": "platform-input-buckets",
    }
    assert report.checked == 8  # AWS::CDK::Metadata is checked too, through the reviewed allow-list
    assert report.helpers == {"CDKMetadata": "allow-list:cdk-metadata"}


def test_repo_inferred_from_owner_repo_tags(matrix):
    report = check_file(TEMPLATES / "financialplanning-beta-ok.json", None, matrix)
    assert report.ok and report.repo == "financialplanning"


def test_non_owner_declaration_fails_and_names_owner(matrix):
    """OWN-01: a FinanceModel template declaring the platform plan table fails, naming resource and owner."""
    report = check_file(TEMPLATES / "financemodel-declares-platform-table.json", "financemodel", matrix)
    assert not report.ok
    [p] = problems(report)
    assert p.logical_id == "PlanTable" and p.owner == "financialplanning"
    assert "financialplanning" in str(p) and "PlanTable" in str(p)
    assert report.matched == {"ResearchBucket": "research-workspace-storage"}


def test_unmapped_resources_fail(matrix):
    """OWN-01: resources with no matrix entry fail (no role, unknown role, type not listed for the role)."""
    report = check_file(TEMPLATES / "financialplanning-unmapped-resource.json", "financialplanning", matrix)
    by_id = {p.logical_id: p.message for p in problems(report)}
    assert set(by_id) == {"WorkQueue", "ScratchBucket", "PlanTableAsBucket"}
    assert "no entry in the ownership matrix" in by_id["WorkQueue"]
    assert "scratch-bucket" in by_id["ScratchBucket"]
    assert "not listed" in by_id["PlanTableAsBucket"]
    assert report.matched == {"RawInputBucket": "platform-input-buckets"}


def test_pipeline_rows_resolve_by_repository(matrix):
    """Pipeline rows share logical roles; the checked repository picks its own row (YAML short-form tags)."""
    report = check_file(TEMPLATES / "financemodel-pipeline-ok.yaml", "financemodel", matrix)
    assert report.ok, [str(p) for p in report.problems]
    assert set(report.matched.values()) == {"pipeline-financemodel"}
    other = check_file(TEMPLATES / "financemodel-pipeline-ok.yaml", "financeagent", matrix)
    assert not other.ok
    assert any(p.owner == "financemodel" for p in other.problems)


def test_shared_tooling_passes(matrix):
    report = check_file(TEMPLATES / "financialplanning-shared-tooling-ok.json", "financialplanning", matrix)
    assert report.ok, [str(p) for p in report.problems]
    assert set(report.matched.values()) == {"pipeline-financialplanning", "project-budget", "contract-registry"}


def test_environment_data_in_shared_resource_fails(matrix):
    """ENV-16: a snapshot bucket tagged environment=shared fails the ownership check."""
    report = check_file(TEMPLATES / "financialplanning-shared-env-data.json", "financialplanning", matrix)
    [p] = problems(report, "ENV-16")
    assert p.logical_id == "SnapshotBucket" and "environment data" in p.message


def test_shared_row_tagged_with_environment_fails(matrix):
    tpl = json.loads((TEMPLATES / "financialplanning-shared-tooling-ok.json").read_text())
    for tag in tpl["Resources"]["PipelineArtifactBucket"]["Properties"]["Tags"]:
        if tag["Key"] == "environment":
            tag["Value"] = "gamma"
    report = check_template(tpl, "financialplanning", matrix)
    assert [p.logical_id for p in problems(report, "ENV-16")] == ["PipelineArtifactBucket"]


def test_preexisting_jev_secret_must_not_be_declared(matrix):
    """ENV-16 / 13.1: the Jev secret is referenced by name only; any IaC declaring it fails."""
    report = check_file(TEMPLATES / "financemodel-declares-jev-secret.json", "financemodel", matrix)
    [p] = problems(report)
    assert p.logical_id == "JevApiKey" and "jev-api-key-secret" in p.message and p.owner == "financemodel"


def test_external_codeconnection_must_not_be_declared(matrix):
    report = check_file(TEMPLATES / "financelambdastool-declares-codeconnection.json", "financelambdastool", matrix)
    [p] = problems(report)
    assert p.logical_id == "GitHubConnection" and p.owner == "external"


def test_owner_repo_tag_mismatch_fails(matrix):
    tpl = json.loads((TEMPLATES / "financialplanning-beta-ok.json").read_text())
    for tag in tpl["Resources"]["PlanTable"]["Properties"]["Tags"]:
        if tag["Key"] == "owner-repo":
            tag["Value"] = "financemodel"
    report = check_template(tpl, "financialplanning", matrix)
    assert any(p.logical_id == "PlanTable" and "owner-repo=financemodel" in p.message for p in report.problems)


def test_unknown_repo_and_untagged_template(matrix):
    assert not check_template({"Resources": {}}, "somewhere", matrix).ok
    report = check_template({"Resources": {"Q": {"Type": "AWS::SQS::Queue"}}}, None, matrix)
    assert not report.ok and "--repo" in report.problems[0].message


def test_map_style_tags_and_metadata_roles(matrix):
    tpl = {
        "Resources": {
            "Catalog": {"Type": "AWS::SSM::Parameter", "Properties": {"Type": "String", "Value": "{}", "Tags": {"logical-role": "tool-catalog", "environment": "beta"}}},
            "Gw": {"Type": "AWS::BedrockAgentCore::Gateway", "Metadata": {"finplan": {"logical-role": "agent-gateway"}}, "Properties": {}},
        }
    }
    assert check_template(tpl, "financelambdastool", matrix).matched == {"Catalog": "tool-catalog"}
    assert check_template({"Resources": {"Gw": tpl["Resources"]["Gw"]}}, "financeagent", matrix).ok


def test_own02_lookups_through_matrix_api(matrix):
    """OWN-02 scenarios answered by the same matrix object the check uses."""
    assert matrix.owner_of("plan-lifecycle-api") == "financialplanning"
    assert matrix.owner_of("sagemaker-job-definitions") == matrix.owner_of("job-interface") == "financemodel"
    assert matrix.owner_of("explanation-provider-config") == "financeagent"
    for rid in ("github-codeconnection", "bedrock-model-access", "market-data-provider-source"):
        assert matrix.owner_of(rid) == "external" and not Matrix.declarable(matrix.by_id[rid])
    assert "finplan/shared/financemodel/jev-api-key" in matrix.secret_names()


def test_cli(capsys):
    assert ownership.main([str(TEMPLATES / "financialplanning-beta-ok.json"), "--repo", "financialplanning"]) == 0
    assert "PASS" in capsys.readouterr().out
    assert ownership.main([str(TEMPLATES / "financemodel-declares-platform-table.json"), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out[0]["problems"][0]["owner"] == "financialplanning"


# ------------------------------------------------- task 1.5: CDK-generated helper resources
LR = "LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a"


def test_cdk_helpers_attributed_to_parent_or_allow_list(matrix):
    """OWN-09 (task 1.5): every CDK-generated helper is attributed (parent construct or reviewed allow-list), none ignored."""
    report = check_file(TEMPLATES / "financemodel-cdk-helpers-ok.json", "financemodel", matrix)
    assert report.ok, [str(p) for p in report.problems]
    assert report.checked == 14
    assert set(report.matched) | set(report.helpers) == set(json.loads((TEMPLATES / "financemodel-cdk-helpers-ok.json").read_text())["Resources"])
    assert report.helpers == {
        "CDKMetadata": "allow-list:cdk-metadata",
        "CustomS3AutoDeleteObjectsCustomResourceProviderHandler9D90184F": "allow-list:cdk-custom-resource-provider",
        "CustomS3AutoDeleteObjectsCustomResourceProviderRole3B1BD092": "allow-list:cdk-custom-resource-provider",
        "JobApiHandlerInvokePermission": "parent:job-control-plane via JobApiHandler",
        "JobApiHandlerLogRetention": "parent:job-control-plane via JobApiHandler",
        "JobApiHandlerServiceRoleDefaultPolicy": "parent:job-control-plane via JobApiHandlerServiceRole,JobControlTable",
        "ResearchBucketAutoDeleteObjectsCustomResource": "parent:research-workspace-storage via ResearchBucket",
        LR: "allow-list:cdk-log-retention-provider",
        LR + "ServiceRole9741ECFB": "allow-list:cdk-log-retention-provider",
        LR + "ServiceRoleDefaultPolicyADDA7DEB": f"parent:{LR}ServiceRole9741ECFB via helper",
    }
    assert "helpers" in report.to_dict()


def test_cdk_helpers_without_owned_parent_or_allow_list_fail(matrix):
    """OWN-09 (task 1.5): no parent in the template, a parent owned by another repo, or an unlisted provider all fail OWN-01."""
    report = check_file(TEMPLATES / "financemodel-cdk-helpers-unattributed.json", "financemodel", matrix)
    by_id = {p.logical_id: p for p in problems(report)}
    assert set(by_id) == {"ImportedRolePolicy", "PlanApiHandler", "PlanApiHandlerInvokePermission", "SomeProviderHandlerABCDEF12"}
    assert "no parent construct" in by_id["ImportedRolePolicy"].message
    assert by_id["PlanApiHandlerInvokePermission"].owner == "financialplanning"
    assert "PlanApiHandler" in by_id["PlanApiHandlerInvokePermission"].message
    assert "no entry in the ownership matrix" in by_id["SomeProviderHandlerABCDEF12"].message
    assert report.helpers == {"CDKMetadata": "allow-list:cdk-metadata"}


def test_cdk_metadata_is_never_silently_ignored(matrix):
    """Without its reviewed allow-list entry, AWS::CDK::Metadata fails like any unmapped resource."""
    data = dict(matrix.data)
    helpers = dict(data["cdk_generated_helpers"])
    helpers["allow_list"] = [e for e in helpers["allow_list"] if e["id"] != "cdk-metadata"]
    data["cdk_generated_helpers"] = helpers
    report = check_file(TEMPLATES / "financialplanning-beta-ok.json", "financialplanning", Matrix(data))
    assert [p.logical_id for p in problems(report)] == ["CDKMetadata"]


def test_helper_type_with_its_own_logical_role_uses_the_normal_path(matrix):
    tpl = json.loads((TEMPLATES / "financemodel-cdk-helpers-ok.json").read_text())
    tpl["Resources"]["JobApiHandlerInvokePermission"]["Metadata"] = {"logical-role": "tool-lambda"}
    report = check_template(tpl, "financemodel", matrix)
    [p] = problems(report)
    assert p.logical_id == "JobApiHandlerInvokePermission" and p.owner == "financelambdastool"


def test_helper_reference_cycle_fails(matrix):
    tpl = {"Resources": {
        "A": {"Type": "AWS::IAM::Policy", "Properties": {"Roles": [{"Ref": "B"}]}},
        "B": {"Type": "AWS::IAM::Policy", "Properties": {"Roles": [{"Ref": "A"}]}},
    }}
    report = check_template(tpl, "financemodel", matrix)
    assert {p.logical_id for p in problems(report)} == {"A", "B"} and not report.helpers


def test_cdk_path_pattern_allow_list_match(matrix):
    data = dict(matrix.data)
    helpers = dict(data["cdk_generated_helpers"])
    helpers["allow_list"] = [{"id": "synthetic-path-entry", "resource_types": ["AWS::Lambda::Function"], "cdk_path_pattern": "/SyntheticProvider/Handler$", "reason": "synthetic test entry", "reviewed": "test"}]
    data["cdk_generated_helpers"] = helpers
    res = {"Type": "AWS::Lambda::Function", "Metadata": {"aws:cdk:path": "Stack/SyntheticProvider/Handler"}, "Properties": {}}
    assert check_template({"Resources": {"X1": res}}, "financemodel", Matrix(data)).helpers == {"X1": "allow-list:synthetic-path-entry"}


@pytest.mark.parametrize("bad, fragment", [
    ({"id": "e", "resource_types": ["AWS::CDK::Metadata"], "logical_id_pattern": "X", "reviewed": "r"}, "no reason"),
    ({"id": "e", "resource_types": ["AWS::CDK::Metadata"], "logical_id_pattern": "X", "reason": "r"}, "no reviewed"),
    ({"id": "e", "resource_types": ["AWS::CDK::Metadata"], "reason": "r", "reviewed": "r"}, "neither"),
    ({"id": "e", "resource_types": [], "logical_id_pattern": "X", "reason": "r", "reviewed": "r"}, "no resource_types"),
    ({"id": "e", "resource_types": ["T"], "logical_id_pattern": "(", "reason": "r", "reviewed": "r"}, "invalid"),
])
def test_allow_list_entries_must_be_explicit_and_reviewed(matrix, bad, fragment):
    data = dict(matrix.data)
    data["cdk_generated_helpers"] = {"parent_attributed_types": [], "allow_list": [bad]}
    with pytest.raises(ValueError, match=fragment):
        Matrix(data)


def test_matrix_allow_list_is_reviewed():
    entries = Matrix.load().helper_allow_list
    assert {e["id"] for e in entries} >= {"cdk-metadata", "cdk-log-retention-provider", "cdk-custom-resource-provider"}
    assert all(e["reason"].strip() and e["reviewed"].strip() for e in entries)


# ------------------------------------------------- 0.2.0 (design D13): the platform's synthesized resource kinds
def test_platform_stack_resource_kinds_have_rows(matrix):
    """OWN-01: API Gateway sub-resources, handler roles and log groups, SSM reference parameters, the
    schedule's dead-letter queue, queue policy and alarm and the metadata sweeper map to owned rows; the
    untaggable API method and the event-invoke config are attributed to their parents. Zero problems."""
    tpl = json.loads((TEMPLATES / "financialplanning-platform-stacks-ok.json").read_text())
    report = check_template(tpl, "financialplanning", matrix)
    assert report.ok, [str(p) for p in report.problems]
    assert set(report.matched) | set(report.helpers) == set(tpl["Resources"])
    m = report.matched
    for lid in ("PlanApi", "PlanApiDeployment", "PlanApiStageLive", "PlanApiV1", "PlanApiV1Portfolios", "PlanApiMissingAuthenticationToken", "PlanApiHandlerRole", "PlanApiLogs", "PlanEndpoint"):
        assert m[lid] == "plan-lifecycle-api", lid
    for lid in ("IngestionRole", "IngestionLogs", "IngestionEndpointParam"):
        assert m[lid] == "ingestion-service", lid
    for lid in ("ScheduleDlq", "ScheduleDlqPolicy", "ScheduleDlqAlarm", "ScheduleRole", "IngestScheduleParam", "DailyIngestSchedule"):
        assert m[lid] == "daily-scheduler", lid
    for lid in ("Sweeper", "SweeperRole", "SweeperLogs", "DailySweep"):
        assert m[lid] == "platform-metadata-sweeper", lid
    assert m["RunStagingRef"] == "run-output-staging-area" and m["BucketParamSnapshots"] == "platform-input-buckets" and m["BucketParamPlans"] == "platform-plan-artifact-buckets"
    assert report.helpers["PlanApiV1PortfoliosPost"].startswith("parent:plan-lifecycle-api")
    assert report.helpers["IngestionFunctionEventInvokeConfig"] == "parent:daily-scheduler,ingestion-service via IngestionFunction,ScheduleDlq"


def test_api_method_of_another_repositorys_api_still_fails(matrix):
    """AWS::ApiGateway::Method is parent-attributed, never ignored: a FinanceModel template declaring a method on the platform API fails."""
    tpl = json.loads((TEMPLATES / "financialplanning-platform-stacks-ok.json").read_text())
    report = check_template(tpl, "financemodel", matrix)
    assert any(p.logical_id == "PlanApiV1PortfoliosPost" and p.owner == "financialplanning" for p in problems(report))


def test_tooling_stack_resource_kinds_have_rows(matrix):
    """OWN-01/ENV-16: permission-boundary policies, budget topic policy and subscriptions, budget roles,
    the pipeline store bucket policy, and environment-tagged deploy/stage roles all pass."""
    tpl = json.loads((TEMPLATES / "financialplanning-tooling-stack-ok.json").read_text())
    report = check_template(tpl, "financialplanning", matrix)
    assert report.ok, [str(p) for p in report.problems]
    m = report.matched
    assert {m[f"{c}PermissionBoundary"] for c in ("Beta", "Gamma", "Prod", "Shared")} == {"permission-boundaries"}
    for lid in ("BudgetAlertTopicPolicy", "BudgetAlertEmail", "BudgetStateWriterSubscription", "BudgetActionRole", "BudgetStateWriterRole"):
        assert m[lid] == "project-budget", lid
    assert m["PipelineStorePolicy"] == m["PipelineRole"] == "pipeline-financialplanning"
    for c in ("Beta", "Gamma", "Prod"):
        for lid in (f"DeployRole{c}", f"DeployExecRole{c}", f"StageRole{c}"):
            assert m[lid] == "pipeline-environment-roles-financialplanning", lid


@pytest.mark.parametrize("env,row", [("shared", "pipeline-financialplanning"), ("gamma", "pipeline-environment-roles-financialplanning")])
def test_deploy_role_row_selected_by_environment_tag(matrix, env, row):
    """ENV-16: a deploy role tagged shared is account-level pipeline tooling; tagged with an environment it is
    that environment's role (and must then carry that environment's boundary, ENV-18). Neither fails."""
    tpl = json.loads((TEMPLATES / "financialplanning-tooling-stack-ok.json").read_text())
    for tag in tpl["Resources"]["DeployRoleGamma"]["Properties"]["Tags"]:
        if tag["Key"] == "environment":
            tag["Value"] = env
    report = check_template(tpl, "financialplanning", matrix)
    assert report.ok, [str(p) for p in report.problems]
    assert report.matched["DeployRoleGamma"] == row


def test_environment_scoped_row_never_tagged_shared(matrix):
    """ENV-16: a per-environment platform resource (the sweeper) tagged shared still fails."""
    tpl = json.loads((TEMPLATES / "financialplanning-platform-stacks-ok.json").read_text())
    for tag in tpl["Resources"]["Sweeper"]["Properties"]["Tags"]:
        if tag["Key"] == "environment":
            tag["Value"] = "shared"
    [p] = problems(check_template(tpl, "financialplanning", matrix), "ENV-16")
    assert p.logical_id == "Sweeper" and "platform-metadata-sweeper" in p.message


def test_select_row_prefers_the_row_of_the_environment_tag(matrix):
    own = [r for r in matrix.rows_for_role("deploy-role") if r["owner"] == "financemodel"]
    assert {r["id"] for r in own} == {"pipeline-financemodel", "pipeline-environment-roles-financemodel"}
    assert ownership.select_row(own, "shared")["id"] == "pipeline-financemodel"
    assert ownership.select_row(own, "prod")["id"] == "pipeline-environment-roles-financemodel"
    assert ownership.select_row(own, None)["id"] == own[0]["id"]
