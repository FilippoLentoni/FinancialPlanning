#!/usr/bin/env python3
"""Generate the synthetic infra test data under tests/data/infra (pipeline, role and shared-resource
templates, job-status documents, release manifests and a cloud assembly) used by the INFRA tests
(ENV-09, ENV-12, ENV-13, ENV-16, ENV-18, ENV-20, OWN-08). Usage: uv run python scripts/gen_infra_test_data.py [OUT_DIR]"""
import copy, json, sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "tests" / "data" / "infra"

def w(rel, doc):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2) + "\n")

def tags(env, role, repo="financialplanning"):
    return [{"Key": "project", "Value": "finplan"}, {"Key": "owner-repo", "Value": repo}, {"Key": "environment", "Value": env}, {"Key": "logical-role", "Value": role}]

def boundary(env):
    return {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/finplan-%s-permission-boundary" % env}

def role(env, lrole, name):
    return {"Type": "AWS::IAM::Role", "Properties": {
        "RoleName": name,
        "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "codepipeline.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
        "PermissionsBoundary": boundary(env), "Tags": tags(env, lrole)}}

def env_stage(env, name=None):
    cap = env.capitalize()
    test_name = "SmokeTests" if env == "prod" else f"{cap}Tests"
    return {"Name": name or cap, "Actions": [
        {"Name": f"Deploy{cap}", "ActionTypeId": {"Category": "Deploy", "Owner": "AWS", "Provider": "CloudFormation", "Version": "1"},
         "Configuration": {"ActionMode": "CREATE_UPDATE", "StackName": f"finplan-{env}-financialplanning-platform", "TemplatePath": "CloudAssembly::platform.template.json", "RoleArn": {"Fn::GetAtt": [f"{cap}CfnExecutionRole", "Arn"]}, "ParameterOverrides": "{\"Environment\": \"%s\"}" % env},
         "InputArtifacts": [{"Name": "CloudAssembly"}], "RoleArn": {"Fn::GetAtt": [f"{cap}DeployRole", "Arn"]}, "RunOrder": 1},
        {"Name": test_name, "ActionTypeId": {"Category": "Test", "Owner": "AWS", "Provider": "CodeBuild", "Version": "1"},
         "Configuration": {"ProjectName": {"Ref": "TestProject"}, "EnvironmentVariables": "[{\"name\":\"FINPLAN_ENV\",\"value\":\"%s\"}]" % env},
         "InputArtifacts": [{"Name": "CloudAssembly"}], "RunOrder": 2}]}

def pipeline_template():
    resources = {
        "Pipeline": {"Type": "AWS::CodePipeline::Pipeline", "Properties": {
            "Name": "finplan-shared-financialplanning-pipeline",
            "PipelineType": "V2",
            "RoleArn": {"Fn::GetAtt": ["PipelineRole", "Arn"]},
            "Variables": [{"Name": "rollback_to_release_id", "DefaultValue": "none", "Description": "Redeploy the stored assembly of this release_id"}],
            "ArtifactStore": {"Type": "S3", "Location": {"Ref": "PipelineArtifactBucket"}},
            "Stages": [
                {"Name": "Source", "Actions": [{"Name": "GitHubMain", "ActionTypeId": {"Category": "Source", "Owner": "AWS", "Provider": "CodeStarSourceConnection", "Version": "1"},
                    "Configuration": {"ConnectionArn": {"Ref": "CodeConnectionRef"}, "FullRepositoryId": "FilippoLentoni/FinancialPlanning", "BranchName": "main"},
                    "OutputArtifacts": [{"Name": "SourceOutput"}], "RunOrder": 1}]},
                {"Name": "Build", "Actions": [{"Name": "BuildTestSynth", "ActionTypeId": {"Category": "Build", "Owner": "AWS", "Provider": "CodeBuild", "Version": "1"},
                    "Configuration": {"ProjectName": {"Ref": "BuildProject"}},
                    "InputArtifacts": [{"Name": "SourceOutput"}], "OutputArtifacts": [{"Name": "CloudAssembly"}, {"Name": "ReleaseMetadata"}], "RunOrder": 1}]},
                env_stage("beta"),
                env_stage("gamma"),
                {"Name": "ManualApproval", "Actions": [{"Name": "ApproveProd", "ActionTypeId": {"Category": "Approval", "Owner": "AWS", "Provider": "Manual", "Version": "1"},
                    "Configuration": {"CustomData": "Approve release #{variables.release_id} for prod"}, "RunOrder": 1}]},
                env_stage("prod"),
            ]}, },
        "BuildProject": {"Type": "AWS::CodeBuild::Project", "Properties": {"Name": "finplan-shared-financialplanning-build", "Source": {"Type": "CODEPIPELINE", "BuildSpec": "version: 0.2\nphases:\n  build:\n    commands:\n      - make test\n      - finplan-conformance conformance\n      - npx cdk synth\n"}, "Artifacts": {"Type": "CODEPIPELINE"}, "Environment": {"Type": "LINUX_CONTAINER", "ComputeType": "BUILD_GENERAL1_SMALL", "Image": "aws/codebuild/standard:7.0"}, "ServiceRole": {"Fn::GetAtt": ["PipelineRole", "Arn"]}, "Tags": tags("shared", "pipeline-build-project")}},
        "TestProject": {"Type": "AWS::CodeBuild::Project", "Properties": {"Name": "finplan-shared-financialplanning-tests", "Source": {"Type": "CODEPIPELINE", "BuildSpec": "version: 0.2\nphases:\n  build:\n    commands:\n      - make integration-test\n"}, "Artifacts": {"Type": "CODEPIPELINE"}, "Environment": {"Type": "LINUX_CONTAINER", "ComputeType": "BUILD_GENERAL1_SMALL", "Image": "aws/codebuild/standard:7.0"}, "ServiceRole": {"Fn::GetAtt": ["PipelineRole", "Arn"]}, "Tags": tags("shared", "pipeline-build-project")}},
        "PipelineArtifactBucket": {"Type": "AWS::S3::Bucket", "Properties": {"Tags": tags("shared", "pipeline-artifact-bucket")}},
        "PipelineRole": role("shared", "pipeline-role", "finplan-shared-financialplanning-pipeline-role"),
    }
    for env in ("beta", "gamma", "prod"):
        cap = env.capitalize()
        resources[f"{cap}DeployRole"] = role(env, "deploy-role", f"finplan-{env}-financialplanning-deploy-role")
        resources[f"{cap}CfnExecutionRole"] = role(env, "deploy-role", f"finplan-{env}-financialplanning-cfn-exec-role")
    return {"AWSTemplateFormatVersion": "2010-09-09",
            "Description": "Synthetic synthesized pipeline template (test fixture).",
            "Metadata": {"synthetic": True},
            "Parameters": {"CodeConnectionRef": {"Type": "AWS::SSM::Parameter::Value<String>", "Default": "/finplan/shared/financialplanning/config/codeconnection-ref"}},
            "Resources": resources}

valid = pipeline_template()
w("pipelines/valid/standard.json", valid)

def stages(t):
    return t["Resources"]["Pipeline"]["Properties"]["Stages"]

t = copy.deepcopy(valid); del stages(t)[4]; w("pipelines/invalid/missing-approval.json", t)
t = copy.deepcopy(valid); s = stages(t); s[2], s[3] = s[3], s[2]; w("pipelines/invalid/gamma-before-beta.json", t)
t = copy.deepcopy(valid); s = stages(t); s.insert(5, s.pop(4)); w("pipelines/invalid/approval-after-prod.json", t)
t = copy.deepcopy(valid); stages(t)[2]["Actions"][1]["InputArtifacts"] = [{"Name": "SourceOutput"}]; w("pipelines/invalid/post-build-uses-source.json", t)
t = copy.deepcopy(valid); stages(t)[3]["Actions"][1]["Configuration"]["ProjectName"] = {"Ref": "BuildProject"}; w("pipelines/invalid/gamma-runs-cdk-synth.json", t)
t = copy.deepcopy(valid); stages(t)[0]["Actions"][0]["Configuration"]["ConnectionArn"] = "arn:aws:codeconnections:us-east-2:<account-id>:connection/example"; w("pipelines/invalid/literal-connection-arn.json", t)
t = copy.deepcopy(valid); del stages(t)[5]["Actions"][0]["RoleArn"]; w("pipelines/invalid/prod-deploy-without-scoped-role.json", t)
t = copy.deepcopy(valid); del stages(t)[5]["Actions"][1]; w("pipelines/invalid/prod-without-smoke-tests.json", t)
t = copy.deepcopy(valid); p = t["Resources"]["Pipeline"]["Properties"]; p["PipelineType"] = "V1"; del p["Variables"]; w("pipelines/invalid/v1-without-rollback-variable.json", t)
t = copy.deepcopy(valid); stages(t)[0]["Actions"][0]["Configuration"]["BranchName"] = "develop"; w("pipelines/invalid/source-not-main.json", t)

# ENV-12: deploy action under the bootstrap caller's identity
t = copy.deepcopy(valid); stages(t)[2]["Actions"][0]["RoleArn"] = "arn:aws:iam::<account-id>:root"; w("pipelines/invalid/deploy-as-caller.json", t)

# ---------------- ENV-18 role templates
def tpl(resources, desc):
    return {"AWSTemplateFormatVersion": "2010-09-09", "Description": desc, "Metadata": {"synthetic": True}, "Resources": resources}

r = role("beta", "plan-api-handler", "finplan-beta-financialplanning-plan-api-handler")
del r["Properties"]["PermissionsBoundary"]
w("roles/invalid/role-without-boundary.json", tpl({"PlanApiHandlerRole": r}, "Role lacking the environment permission boundary (ENV-18)."))
r = role("beta", "plan-api-handler", "finplan-beta-financialplanning-plan-api-handler"); r["Properties"]["PermissionsBoundary"] = boundary("gamma")
w("roles/invalid/role-with-other-environment-boundary.json", tpl({"PlanApiHandlerRole": r}, "Beta role carrying the gamma boundary (ENV-18)."))
r = role("beta", "plan-api-handler", "finplan-beta-financialplanning-plan-api-handler")
w("roles/valid/role-with-environment-boundary.json", tpl({"PlanApiHandlerRole": r}, "Beta role with the beta permission boundary."))
rr = role("gamma", "research-job-role", "finplan-gamma-financemodel-research-job")
rr["Properties"]["PermissionsBoundary"] = {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/finplan-gamma-research-permission-boundary"}
w("roles/valid/research-role-with-research-boundary.json", tpl({"ResearchJobRole": rr}, "FinanceModel gamma research role with the research boundary."))
pol = {"Type": "AWS::IAM::ManagedPolicy", "Properties": {"ManagedPolicyName": "finplan-prod-permission-boundary", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}}}
r = role("prod", "plan-api-handler", "finplan-prod-financialplanning-plan-api-handler"); r["Properties"]["PermissionsBoundary"] = {"Ref": "ProdBoundary"}
w("roles/valid/role-with-boundary-ref.json", tpl({"ProdBoundary": pol, "PlanApiHandlerRole": r}, "Prod role referencing a boundary declared in the same template."))

# ---------------- ENV-16 shared resources
w("shared/valid/pipeline-artifact-bucket.json", tpl({"PipelineArtifactBucket": {"Type": "AWS::S3::Bucket", "Properties": {"Tags": tags("shared", "pipeline-artifact-bucket")}}}, "Shared pipeline artifact bucket (allowed)."))
w("shared/valid/beta-snapshot-bucket.json", tpl({"SnapshotBucket": {"Type": "AWS::S3::Bucket", "Properties": {"Tags": tags("beta", "snapshot-artifact-bucket")}}}, "Per-environment snapshot bucket (allowed)."))
w("shared/invalid/snapshot-bucket-tagged-shared.json", tpl({"SnapshotBucket": {"Type": "AWS::S3::Bucket", "Properties": {"Tags": tags("shared", "snapshot-artifact-bucket")}}}, "Environment data (snapshots) in a shared resource (ENV-16)."))
w("shared/invalid/plan-table-tagged-shared.json", tpl({"PlanTable": {"Type": "AWS::DynamoDB::Table", "Properties": {"KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}], "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}], "BillingMode": "PAY_PER_REQUEST", "Tags": tags("shared", "plan-table")}}}, "Plan metadata table in a shared resource (ENV-16)."))
w("shared/invalid/run-outputs-in-shared-artifact-bucket.json", tpl({"PipelineArtifactBucket": {"Type": "AWS::S3::Bucket", "Properties": {"LifecycleConfiguration": {"Rules": [{"Id": "beta-run-outputs", "Prefix": "beta/run-outputs/", "Status": "Enabled", "ExpirationInDays": 30}]}, "Tags": tags("shared", "pipeline-artifact-bucket")}}}, "Shared bucket holding beta run outputs (ENV-16)."))
w("shared/invalid/jev-secret-declared.json", tpl({"JevApiKey": {"Type": "AWS::SecretsManager::Secret", "Properties": {"Name": "finplan/shared/financemodel/jev-api-key", "Tags": tags("shared", "jev-api-key", "financemodel")}}}, "IaC declaring the pre-existing Jev API key secret (ENV-16)."))
w("shared/invalid/untagged-role-shared-unknown.json", tpl({"Mystery": {"Type": "AWS::S3::Bucket", "Properties": {"Tags": tags("shared", "scratch-bucket")}}}, "Shared resource with no ownership-matrix row."))

# ---------------- ENV-20 job status documents
base = {
    "run_id": "run_01KDVDNBYGX5V5HY2JSK5XWKHC", "state": "awaiting_approval", "purpose": "tuning", "dry_run": False, "compute_class": "gpu",
    "cost_estimate": {"estimated_usd_upper_bound": 4.5, "price_retrieved_at": "2026-01-10T09:00:00Z", "remaining_allocation_usd": 25, "budget_category": "gpu", "synthetic": True},
    "transitions": [{"state": "awaiting_approval", "at": "2026-01-10T09:00:00Z"}],
    "configuration_id": "cfg_9eda1821d7a3f8b5c965f4375058e544a3aba022644d6a94d9e446a63100a5f0",
    "input_snapshot_id": "snap_01KDVDNAZ83BAMMYCEGWF33DPM", "domain": "finance",
    "submitted_at": "2026-01-10T09:00:00Z", "updated_at": "2026-01-10T09:06:00Z", "synthetic": True}
appr = {"approved_by": "synthetic-approver", "approved_at": "2026-01-10T10:00:00Z", "approved_estimate_usd": 4.5}
def js(rel, **kw):
    d = copy.deepcopy(base); 
    for k, v in kw.items():
        if v is None: d.pop(k, None)
        else: d[k] = v
    w(rel, d)
js("job-status/valid/gpu-awaiting-approval.json")
js("job-status/valid/gpu-approved-running.json", state="running", approval=appr, transitions=[{"state": "awaiting_approval", "at": "2026-01-10T09:00:00Z"}, {"state": "queued", "at": "2026-01-10T10:01:00Z"}, {"state": "starting", "at": "2026-01-10T10:02:00Z"}, {"state": "running", "at": "2026-01-10T10:05:00Z"}], updated_at="2026-01-10T10:05:00Z")
js("job-status/valid/cpu-queued-without-approval.json", state="queued", compute_class="cpu", cost_estimate={**base["cost_estimate"], "budget_category": "cpu_research"}, transitions=[{"state": "queued", "at": "2026-01-10T09:00:00Z"}])
js("job-status/invalid/gpu-queued-without-approval.json", state="queued", transitions=[{"state": "awaiting_approval", "at": "2026-01-10T09:00:00Z"}, {"state": "queued", "at": "2026-01-10T09:05:00Z"}])
js("job-status/invalid/gpu-running-without-approval.json", state="running", transitions=[{"state": "running", "at": "2026-01-10T09:05:00Z"}])
js("job-status/invalid/gpu-category-only-queued-without-approval.json", state="queued", compute_class=None, transitions=[])
js("job-status/invalid/gpu-history-left-awaiting-without-approval.json", transitions=[{"state": "awaiting_approval", "at": "2026-01-10T09:00:00Z"}, {"state": "starting", "at": "2026-01-10T09:02:00Z"}, {"state": "awaiting_approval", "at": "2026-01-10T09:03:00Z"}])
js("job-status/invalid/gpu-left-before-approval.json", state="running", approval=appr, transitions=[{"state": "awaiting_approval", "at": "2026-01-10T09:00:00Z"}, {"state": "queued", "at": "2026-01-10T09:30:00Z"}, {"state": "running", "at": "2026-01-10T10:30:00Z"}])
js("job-status/invalid/gpu-estimate-above-approved.json", state="queued", approval={**appr, "approved_estimate_usd": 3.0}, transitions=[{"state": "awaiting_approval", "at": "2026-01-10T09:00:00Z"}, {"state": "queued", "at": "2026-01-10T10:01:00Z"}])
js("job-status/invalid/gpu-succeeded-without-approval.json", state="succeeded", completion_status="succeeded", transitions=[])

# ---------------- OWN-08 manifests
def manifest(repo, env, rel, cv, served, outputs, digest_seed):
    d = {"repo": repo, "environment": env, "region": "us-east-2", "release_id": rel,
         "source_commit": ("ab" + digest_seed * 19)[:40],
         "artifact_digest": "sha256:" + ("cd" + digest_seed * 31)[:64],
         "contract_version": cv, "deployed_at": "2026-01-15T14:30:00Z", "previous_release_id": None,
         "outputs": outputs, "served_contract_majors": served, "synthetic": True}
    if env == "prod":
        d["approved_by"] = "synthetic-approver"; d["approved_at"] = "2026-01-16T14:30:00Z"
    return d
G = "gamma"
fp = manifest("financialplanning", G, "rel_01KDVDNBYGX5V5HY2JSK5XWKHC", "1.0.0", [1], {"plan-endpoint": "/finplan/gamma/financialplanning/api/plan-endpoint"}, "a1")
fm = manifest("financemodel", G, "rel_01KDVDNAZ83BAMMYCEGWF33DPM", "1.2.0", [1], {"job-endpoint": "/finplan/gamma/financemodel/api/job-endpoint"}, "b2")
lt = manifest("financelambdastool", G, "rel_01KDVDNA004HW7SQ5T1D09E9D2", "1.1.0", [1], {"tool-catalog": "/finplan/gamma/financelambdastool/contract/tool-catalog"}, "c3")
fa = manifest("financeagent", G, "rel_01KDVDNCQ8JZ3T9W4XY5Z6A7B8", "1.0.0", [], {"gateway-principal-ref": "/finplan/gamma/financeagent/agent/gateway-principal-ref"}, "d4")
for m in (fp, fm, lt, fa):
    w(f"manifests/gamma-compatible/{m['repo']}.json", m)
lt2 = dict(lt, contract_version="2.0.0", served_contract_majors=[2])
for m in (fp, fm, lt2, fa):
    w(f"manifests/gamma-incompatible-major/{m['repo']}.json", m)
fp_both = dict(fp, served_contract_majors=[1, 2]); fm_both = dict(fm, served_contract_majors=[1, 2]); lt_both = dict(lt2, served_contract_majors=[1, 2])
for m in (fp_both, fm_both, lt_both, fa):
    w(f"manifests/gamma-two-majors-served/{m['repo']}.json", m)
for m in (fp, fm, lt):
    w(f"manifests/gamma-agent-missing/{m['repo']}.json", m)
fm_beta = dict(fm, environment="beta", outputs={"job-endpoint": "/finplan/beta/financemodel/api/job-endpoint"})
for m in (fp, fm_beta, lt, fa):
    w(f"manifests/gamma-mixed-environments/{m['repo']}.json", m)

# ---------------- bootstrap cloud assembly
w("assembly/manifest.json", {"version": "36.0.0", "artifacts": {
    "FinplanToolingStack": {"type": "aws:cloudformation:stack", "environment": "aws://unknown-account/us-east-2", "properties": {"templateFile": "FinplanToolingStack.template.json", "stackName": "finplan-shared-financialplanning-tooling"}},
    "FinplanPipelineStack": {"type": "aws:cloudformation:stack", "environment": "aws://unknown-account/us-east-2", "properties": {"templateFile": "FinplanPipelineStack.template.json", "stackName": "finplan-shared-financialplanning-pipeline"}},
    "Tree": {"type": "cdk:tree", "properties": {"file": "tree.json"}}}})
w("assembly/FinplanPipelineStack.template.json", valid)

# tooling stack = budget template + permission boundaries
from finplan_contracts import budget as _b, boundaries as _bd
tool = copy.deepcopy(_b.budget_template())
for name, doc in _bd.generate_templates().items():
    if name.startswith("permission-boundary") or name.startswith("research-permission-boundary"):
        tool["Resources"].update(doc["Resources"])
tool["Metadata"]["synthetic"] = True
w("assembly/FinplanToolingStack.template.json", tool)
