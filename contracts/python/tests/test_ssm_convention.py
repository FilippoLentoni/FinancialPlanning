"""ENV-07 / ENV-08 / 12.2 / 13.2: SSM naming convention, registered keys and write rules (task 8.2)."""

from __future__ import annotations

import json

import pytest

from finplan_contracts import ssm
from finplan_contracts.ssm import Writer, build, check_read, check_write, find_registered, find_retired, parse, validate_value

from conftest import fixture

SPEC_EXAMPLES = [
    "/finplan/gamma/financelambdastool/lambda/refresh-market-data-arn",
    "/finplan/shared/financialplanning/contract/registry-ref",
    "/finplan/prod/financialplanning/release/current-release-id",
    "/finplan/prod/financialplanning/release/manifest",
    "/finplan/shared/financemodel/secret-ref/jev-api-key",
    "/finplan/beta/financeagent/config/explanation-model-id",
    "/finplan/gamma/financeagent/config/explanation-provider",
    "/finplan/shared/financialplanning/config/budget-allocation",
    "/finplan/shared/financialplanning/config/cost-ceiling-usd",
    "/finplan/shared/financialplanning/config/budget-state",
    "/finplan/beta/financemodel/config/budget-enforced-role-names",
    "/finplan/gamma/financeagent/agent/gateway-principal-ref",
    "/finplan/prod/financelambdastool/contract/tool-catalog",
    "/finplan/shared/financemodel/config/codeconnection-ref",
    # 0.2.0 (design D13; decisions 15a/15b of 2026-10-07)
    "/finplan/gamma/financeagent/agent/user-pool-ref",
    "/finplan/gamma/financeagent/agent/authorizer-metadata-ref",
    "/finplan/gamma/financeagent/secret-ref/ci-test-client",
    "/finplan/prod/financelambdastool/config/direct-test-principal-name",
    "/finplan/beta/financemodel/job/job-role-ref",
    "/finplan/beta/financemodel/job/job-api-role-ref",
]


@pytest.mark.parametrize("path", SPEC_EXAMPLES)
def test_valid_paths_parse_and_are_registered(path):
    p = parse(path)
    assert p.path == path
    assert find_registered(path) is not None, path


def test_build_round_trip():
    assert build("gamma", "financelambdastool", "lambda", "refresh-market-data-arn") == SPEC_EXAMPLES[0]
    assert build("shared", "financialplanning", "contract", "registry-ref") == SPEC_EXAMPLES[1]


@pytest.mark.parametrize(
    "path,reason",
    [
        ("/finplan/gamma/financelambdastool/secrets/x", "category"),
        ("/finplan/gamma/financelambdastool/endpoint/x", "category"),
        ("/finplan/dev/financemodel/api/job-endpoint", "environment"),
        ("/finplan/gamma/FinanceModel/api/job-endpoint", "repo"),
        ("/finplan/gamma/website/api/x", "repo"),
        ("/finplan/gamma/financemodel/api/Job_Endpoint", "kebab-case"),
        ("/finplan/gamma/financemodel/api", "5 segments"),
        ("/finplan/gamma/financemodel/api/job/endpoint", "5 segments"),
        ("/other/gamma/financemodel/api/job-endpoint", "first segment"),
        ("finplan/gamma/financemodel/api/job-endpoint", "start with"),
    ],
)
def test_rejected_names_and_categories(path, reason):
    errors = ssm.validation_errors(path)
    assert errors and any(reason in e for e in errors), errors
    with pytest.raises(ssm.SsmNameError):
        parse(path)


def test_category_enum_matches_the_contract_pattern(store):
    pattern = store.get("common").schema["$defs"]["ssm_parameter_name"]["pattern"]
    for c in ssm.CATEGORIES:
        assert f"|{c}|" in pattern.replace("(", "|").replace(")", "|")
    assert ssm.ENVIRONMENTS == tuple(store.get("common").schema["$defs"]["environment"]["enum"])


def test_cross_repo_write_rejected():
    d = check_write("/finplan/gamma/financialplanning/config/run-staging-ref", Writer("financemodel"))
    assert not d and "cross-repo" in d.reasons[0]


def test_own_repo_write_allowed_and_env_bound_writer():
    assert check_write("/finplan/gamma/financemodel/api/job-endpoint", Writer("financemodel"))
    assert check_write("/finplan/gamma/financemodel/api/job-endpoint", Writer("financemodel", environment="gamma"))
    assert not check_write("/finplan/prod/financemodel/api/job-endpoint", Writer("financemodel", environment="gamma"))


def test_shared_writes_only_by_bootstrap_or_contract_publish():
    assert not check_write("/finplan/shared/financialplanning/contract/registry-ref", Writer("financialplanning", "pipeline"))
    assert check_write("/finplan/shared/financialplanning/contract/registry-ref", Writer("financialplanning", "contract-publish"))
    assert check_write("/finplan/shared/financialplanning/contract/registry-ref", Writer("financialplanning", "bootstrap"))
    assert check_write("/finplan/shared/financemodel/config/codeconnection-ref", Writer("financemodel", "bootstrap"))
    assert not check_write("/finplan/shared/financemodel/config/codeconnection-ref", Writer("financemodel", "contract-publish"))
    assert check_write("/finplan/shared/financemodel/secret-ref/jev-api-key", Writer("financemodel", "bootstrap"))
    assert not check_write("/finplan/shared/financemodel/secret-ref/jev-api-key", Writer("financialplanning", "bootstrap"))


def test_budget_state_is_the_only_shared_runtime_writer():
    writer = Writer("financialplanning", "runtime", principal="budget-state-writer")
    assert check_write(ssm.BUDGET_STATE_PARAMETER, writer)
    for path in (
        "/finplan/shared/financialplanning/config/budget-allocation",
        "/finplan/shared/financialplanning/config/cost-ceiling-usd",
        "/finplan/shared/financialplanning/contract/registry-ref",
        "/finplan/beta/financialplanning/config/run-staging-ref",
    ):
        assert not check_write(path, writer), path
    for other in (Writer("financialplanning", "runtime", principal="plan-api-handler"), Writer("financemodel", "runtime", principal="budget-state-writer")):
        assert not check_write(ssm.BUDGET_STATE_PARAMETER, other)


@pytest.mark.parametrize(
    "path",
    [
        "/finplan/beta/financemodel/config/budget-allocation",
        "/finplan/gamma/financeagent/config/budget-allocation",
        "/finplan/prod/financelambdastool/config/budget-allocation",
        "/finplan/beta/financeagent/secret-ref/openai-api-key",
        "/finplan/prod/financeagent/secret-ref/openai-api-key",
    ],
)
def test_retired_keys_rejected_for_new_writes(path):
    repo = parse(path).repo
    d = check_write(path, Writer(repo))
    assert not d and any("retired" in r for r in d.reasons)
    assert find_retired(path) is not None and find_registered(path) is None


def test_single_budget_allocation_key_is_not_retired():
    assert find_retired("/finplan/shared/financialplanning/config/budget-allocation") is None
    assert find_registered("/finplan/shared/financialplanning/config/budget-allocation").key == "budget-allocation"


def test_reads_same_environment_and_shared_only():
    assert check_read("/finplan/gamma/financelambdastool/lambda/refresh-market-data-arn", "gamma")
    assert check_read("/finplan/shared/financialplanning/contract/registry-ref", "gamma")
    assert not check_read("/finplan/prod/financialplanning/api/plan-endpoint", "gamma")


# ---------------------------------------------------------------- values (ENV-08 and D11 keys)
def test_jev_secret_ref_holds_only_the_name():
    path = "/finplan/shared/financemodel/secret-ref/jev-api-key"
    assert validate_value(path, "finplan/shared/financemodel/jev-api-key") == []
    assert validate_value(path, "arn:aws:secretsmanager:us-east-2:<account-id>:secret:finplan/shared/financemodel/jev-api-key")
    assert validate_value(path, "sk-synthetic-not-a-real-key-0000")
    assert validate_value(path, "finplan/shared/financemodel/other-key")


def test_explanation_provider_and_model_values():
    assert validate_value("/finplan/beta/financeagent/config/explanation-provider", "bedrock") == []
    assert validate_value("/finplan/prod/financeagent/config/explanation-model-id", "us.anthropic.claude-opus-5") == []
    assert validate_value("/finplan/prod/financeagent/config/explanation-model-id", "arn:aws:bedrock:us-east-2::foundation-model/x")


def test_budget_allocation_value_validated_against_contract():
    path = "/finplan/shared/financialplanning/config/budget-allocation"
    assert validate_value(path, json.dumps(fixture("budget-allocation/valid/defaults.json"))) == []
    assert validate_value(path, json.dumps(fixture("budget-allocation/invalid/sum-above-ceiling.json")))
    assert validate_value(path, json.dumps({"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 25, "reserve": 5}), cost_ceiling_usd=40)


def test_enforced_role_names_and_manifest_values():
    path = "/finplan/gamma/financeagent/config/budget-enforced-role-names"
    assert validate_value(path, "finplan-gamma-financeagent-runtime-role,finplan-gamma-financeagent-deploy-role") == []
    assert validate_value(path, json.dumps(["finplan-gamma-financeagent-runtime-role"])) == []
    assert validate_value(path, "arn:aws:iam::<account-id>:role/x")
    manifest = fixture("release-manifest/valid/gamma-financelambdastool.json")
    assert validate_value("/finplan/gamma/financelambdastool/release/manifest", json.dumps(manifest)) == []
    assert validate_value("/finplan/beta/financelambdastool/release/manifest", json.dumps(manifest))


def test_cost_allocation_tag_keys_registered(store):
    tags = ssm.cost_allocation_tags("financemodel", "gamma", "research-job-role", run_id="run_01KDVDNBYGX5V5HY2JSK5XWKHC")
    assert set(tags) == set(ssm.COST_ALLOCATION_TAG_KEYS)
    from finplan_contracts.validate import validate

    assert validate(tags, "cost-allocation-tags").valid
    assert set(store.get("cost-allocation-tags").schema["properties"]) == set(ssm.COST_ALLOCATION_TAG_KEYS)


def test_cli(capsys):
    assert ssm.main(["build", "--env", "gamma", "--repo", "financemodel", "--category", "api", "--name", "job-endpoint"]) == 0
    assert capsys.readouterr().out.strip() == "/finplan/gamma/financemodel/api/job-endpoint"
    assert ssm.main(["check", "/finplan/gamma/financialplanning/api/plan-endpoint", "--writer-repo", "financemodel"]) == 1
    assert ssm.main(["check", "/finplan/beta/financeagent/secret-ref/openai-api-key", "--writer-repo", "financeagent"]) == 1
    assert ssm.main(["keys"]) == 0
