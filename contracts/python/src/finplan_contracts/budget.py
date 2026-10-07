"""Project cost ceiling, category allocation, pre-flight checks and the budget templates.

Design D4/D10/D11; spec environment-promotion "Project cost ceiling" (ENV-17) and
"Budget alerts and enforcement action" (ENV-19); tasks 13.3 (semantic part) and 13.4.

Pre-flight (ENV-17)
-------------------
:func:`preflight` is the check every paying repository runs before paid work:

* the allocation (``/finplan/shared/financialplanning/config/budget-allocation``)
  must validate against ``core/v1/budget-allocation.json`` and sum to at most the
  ceiling (``/finplan/shared/financialplanning/config/cost-ceiling-usd``); an
  invalid allocation refuses all paid work (``VALIDATION_FAILED``);
* when the budget action has fired (``budget-state`` says ``enforced``) all paid
  work is refused with ``BUDGET_EXCEEDED``;
* the job's ``estimated_usd_upper_bound`` must fit the remaining allocation of its
  ``budget_category`` or the job is refused with ``BUDGET_EXCEEDED``.

Remaining allocation counts only AWS spend: cost records billed by a third party
(TypeSafe Jev prepaid credits) are reported separately and never counted against
the ceiling or any category (:func:`aws_spend_by_category`).

Templates (ENV-19)
------------------
:func:`budget_template` is the CloudFormation fragment for the FinancialPlanning
tooling stack (``shared``): one AWS Budgets budget whose limit comes from the
cost-ceiling parameter, notifications at 50/80/100% of ACTUAL spend, and a budget
action at 100% that applies :func:`enforcement_deny_policy` to every role name
published at ``/finplan/<env>/<repo>/config/budget-enforced-role-names`` (this
includes the FinanceAgent runtime role, so Bedrock invocations stop). The action
uses ``ApprovalModel: AUTOMATIC``; reverting it is a human step (every
permission boundary denies ``budgets:ExecuteBudgetAction`` and detaching the deny
policy). CDK wiring happens in the add-platform-foundation change.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import ssm as _ssm

__all__ = [
    "CATEGORIES",
    "DEFAULT_ALLOCATION",
    "DEFAULT_CEILING_USD",
    "ALERT_THRESHOLDS_PERCENT",
    "ENFORCEMENT_THRESHOLD_PERCENT",
    "ENFORCED_DENY_ACTIONS",
    "CostRecord",
    "PreflightResult",
    "allocation_problems",
    "aws_spend_by_category",
    "third_party_spend",
    "remaining_by_category",
    "preflight",
    "enforcement_deny_policy",
    "budget_template",
    "generate_templates",
    "main",
]

CEILING_PARAMETER = "/finplan/shared/financialplanning/config/cost-ceiling-usd"
ALLOCATION_PARAMETER = "/finplan/shared/financialplanning/config/budget-allocation"
STATE_PARAMETER = _ssm.BUDGET_STATE_PARAMETER
ALERT_THRESHOLDS_PERCENT = (50, 80, 100)
ENFORCEMENT_THRESHOLD_PERCENT = 100
AWS_BILLER = "aws"


def _schema_defaults() -> tuple[tuple[str, ...], dict[str, float], float]:
    """Categories, default allocation and default ceiling, from the contract schema (single source)."""
    from .schemas import load_store

    schema = load_store().get("budget-allocation").schema
    cats = tuple(schema["$defs"]["category"]["enum"])
    return cats, dict(schema["x-finplan-default-allocation"]), float(schema["x-finplan-default-ceiling-usd"])


CATEGORIES, DEFAULT_ALLOCATION, DEFAULT_CEILING_USD = _schema_defaults()

#: New billable compute, model invocations and pipeline executions blocked at 100% (reads stay allowed).
ENFORCED_DENY_ACTIONS: list[str] = [
    "sagemaker:CreateTrainingJob",
    "sagemaker:CreateProcessingJob",
    "sagemaker:CreateTransformJob",
    "sagemaker:CreateHyperParameterTuningJob",
    "sagemaker:CreateEndpoint",
    "sagemaker:CreateEndpointConfig",
    "sagemaker:UpdateEndpoint",
    "sagemaker:StartPipelineExecution",
    "sagemaker:CreateNotebookInstance",
    "sagemaker:StartNotebookInstance",
    "codepipeline:StartPipelineExecution",
    "codepipeline:RetryStageExecution",
    "codebuild:StartBuild",
    "codebuild:StartBuildBatch",
    "codebuild:RetryBuild",
    "bedrock:InvokeModel*",
    "bedrock:Converse*",
    "bedrock:InvokeAgent",
    "bedrock-agentcore:InvokeAgentRuntime",
    "ec2:RunInstances",
    "ec2:StartInstances",
    "ecs:RunTask",
    "ecs:StartTask",
    "batch:SubmitJob",
    "states:StartExecution",
    "states:StartSyncExecution",
    "scheduler:CreateSchedule",
]


# ===================================================================== allocation
def allocation_problems(allocation: Any, ceiling_usd: float | None = None) -> list[str]:
    """Problems with an allocation map (schema + sum at most the ceiling); empty when valid."""
    from .validate import validate

    ceiling = DEFAULT_CEILING_USD if ceiling_usd is None else ceiling_usd
    res = validate(allocation, "budget-allocation", context={"cost_ceiling_usd": ceiling})
    return [i.message for i in res.issues]


@dataclass(frozen=True)
class CostRecord:
    """A spend record. ``billed_by`` is ``aws`` for AWS spend; anything else (``typesafe``) is third-party."""

    usd: float
    category: str | None = None
    billed_by: str = AWS_BILLER
    description: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CostRecord":
        return cls(usd=float(d["usd"]), category=d.get("category") or d.get("budget_category"), billed_by=str(d.get("billed_by", AWS_BILLER)), description=str(d.get("description", "")))


def _records(records: Iterable[CostRecord | Mapping[str, Any]]) -> list[CostRecord]:
    return [r if isinstance(r, CostRecord) else CostRecord.from_dict(r) for r in records]


def aws_spend_by_category(records: Iterable[CostRecord | Mapping[str, Any]]) -> dict[str, float]:
    """AWS spend per category. Third-party records (Jev / TypeSafe prepaid credits) are excluded."""
    out = {c: 0.0 for c in CATEGORIES}
    for r in _records(records):
        if r.billed_by != AWS_BILLER:
            continue
        if r.category not in out:
            raise ValueError(f"AWS cost record has unregistered budget category {r.category!r}")
        out[r.category] += r.usd
    return out


def third_party_spend(records: Iterable[CostRecord | Mapping[str, Any]]) -> dict[str, float]:
    """Spend billed outside AWS, by biller (tracked separately, not part of the ceiling)."""
    out: dict[str, float] = {}
    for r in _records(records):
        if r.billed_by != AWS_BILLER:
            out[r.billed_by] = out.get(r.billed_by, 0.0) + r.usd
    return out


def remaining_by_category(allocation: Mapping[str, float], records: Iterable[CostRecord | Mapping[str, Any]] = ()) -> dict[str, float]:
    spent = aws_spend_by_category(records)
    return {c: float(allocation.get(c, 0.0)) - spent.get(c, 0.0) for c in CATEGORIES}


# ===================================================================== pre-flight
@dataclass
class PreflightResult:
    allowed: bool
    budget_category: str | None
    estimated_usd_upper_bound: float | None
    remaining_allocation_usd: float | None
    error: dict[str, Any] | None = None
    third_party_usd: dict[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed


def _contract_version() -> str:
    from .schemas import load_store

    return load_store().version


def _error(code: str, message: str, details: dict[str, Any], correlation_id: str | None) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": False,
        "details": details,
        "correlation_id": correlation_id or f"corr-{uuid.uuid4().hex[:16]}",
        "contract_version": _contract_version(),
    }


def _state_enforced(state: Any) -> bool:
    if state is None:
        return False
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except json.JSONDecodeError:
            return state.strip().lower() == "enforced"
    if isinstance(state, Mapping):
        return bool(state.get("enforced")) or str(state.get("state", "")).lower() == "enforced"
    return False


def preflight(
    budget_category: str,
    estimated_usd_upper_bound: float,
    allocation: Mapping[str, float] | None = None,
    spend_records: Iterable[CostRecord | Mapping[str, Any]] = (),
    *,
    ceiling_usd: float | None = None,
    budget_state: Any = None,
    correlation_id: str | None = None,
) -> PreflightResult:
    """Pre-flight check of paid work against its category's remaining allocation (ENV-17)."""
    allocation = dict(DEFAULT_ALLOCATION if allocation is None else allocation)
    records = _records(spend_records)
    third = third_party_spend(records)
    if budget_category not in CATEGORIES:
        err = _error("VALIDATION_FAILED", f"unregistered budget category {budget_category!r}", {"pointer": "/budget_category", "field": "budget_category"}, correlation_id)
        return PreflightResult(False, budget_category, estimated_usd_upper_bound, None, err, third)
    problems = allocation_problems(allocation, ceiling_usd)
    if problems:
        err = _error(
            "VALIDATION_FAILED",
            "budget allocation is invalid; no paid work starts",
            {"pointer": "", "parameter": ALLOCATION_PARAMETER, "problems": problems},
            correlation_id,
        )
        return PreflightResult(False, budget_category, estimated_usd_upper_bound, None, err, third)
    remaining = remaining_by_category(allocation, records)[budget_category]
    if _state_enforced(budget_state):
        err = _error(
            "BUDGET_EXCEEDED",
            "the project budget enforcement action is active; new paid work is refused",
            {"budget_category": budget_category, "estimated_usd_upper_bound": estimated_usd_upper_bound, "remaining_allocation_usd": remaining, "budget_state": "enforced"},
            correlation_id,
        )
        return PreflightResult(False, budget_category, estimated_usd_upper_bound, remaining, err, third)
    if estimated_usd_upper_bound < 0:
        err = _error("VALIDATION_FAILED", "estimated_usd_upper_bound must be >= 0", {"pointer": "/estimated_usd_upper_bound"}, correlation_id)
        return PreflightResult(False, budget_category, estimated_usd_upper_bound, remaining, err, third)
    if estimated_usd_upper_bound > remaining + 1e-9:
        err = _error(
            "BUDGET_EXCEEDED",
            f"estimated cost exceeds the remaining {budget_category} allocation",
            {"budget_category": budget_category, "estimated_usd_upper_bound": estimated_usd_upper_bound, "remaining_allocation_usd": round(remaining, 6)},
            correlation_id,
        )
        return PreflightResult(False, budget_category, estimated_usd_upper_bound, remaining, err, third)
    return PreflightResult(True, budget_category, estimated_usd_upper_bound, remaining, None, third)


# ===================================================================== templates
def enforcement_deny_policy() -> dict[str, Any]:
    """Deny policy the budget action attaches at 100% (blocks new billable work; reads stay allowed)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "DenyNewBillableWorkAtBudgetCeiling", "Effect": "Deny", "Action": list(ENFORCED_DENY_ACTIONS), "Resource": "*"},
        ],
    }


def _role_names_parameter_id(env: str, repo: str) -> str:
    return f"EnforcedRoleNames{env.capitalize()}{repo.capitalize()}"


def budget_template() -> dict[str, Any]:
    """CloudFormation fragment: project budget, alerts, enforcement action, deny policy (ENV-17, ENV-19)."""
    from .boundaries import BUDGET_DENY_POLICY_NAME, boundary_name

    tags = _ssm.cost_allocation_tags("financialplanning", "shared", "project-budget")
    tag_list = lambda role: [{"Key": k, "Value": v} for k, v in {**tags, "logical-role": role}.items()]  # noqa: E731
    params: dict[str, Any] = {
        "CostCeilingUsd": {
            "Type": "AWS::SSM::Parameter::Value<String>",
            "Default": CEILING_PARAMETER,
            "Description": "Project cost ceiling in USD, read from SSM (initially 50).",
        },
        "NotificationEmail": {"Type": "String", "Description": "Human-configured budget notification target (kept in local untracked configuration).", "MinLength": 3},
        "BudgetTimeUnit": {"Type": "String", "Default": "ANNUALLY", "AllowedValues": ["MONTHLY", "QUARTERLY", "ANNUALLY"]},
    }
    role_lists = []
    for env in _ssm.ENVIRONMENTS:
        for repo in _ssm.REPOS:
            pid = _role_names_parameter_id(env, repo)
            params[pid] = {
                "Type": "AWS::SSM::Parameter::Value<List<String>>",
                "Default": _ssm.build(env, repo, "config", "budget-enforced-role-names"),
                "Description": f"Role names {repo} publishes for the {env} budget action (includes the FinanceAgent runtime role for Bedrock).",
            }
            role_lists.append({"Fn::Join": [",", {"Ref": pid}]})
    notifications = [
        {
            "Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN", "Threshold": t, "ThresholdType": "PERCENTAGE"},
            "Subscribers": [{"SubscriptionType": "EMAIL", "Address": {"Ref": "NotificationEmail"}}, {"SubscriptionType": "SNS", "Address": {"Ref": "BudgetAlertTopic"}}],
        }
        for t in ALERT_THRESHOLDS_PERCENT
    ]
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Project budget (USD ceiling from SSM), alerts at 50/80/100% of actual spend and a deny action at 100% on the published enforced role names (design D10/D11, ENV-17, ENV-19). Part of the FinancialPlanning tooling stack (environment shared).",
        "Parameters": params,
        "Resources": {
            "ProjectBudget": {
                "Type": "AWS::Budgets::Budget",
                "Properties": {
                    "Budget": {
                        "BudgetName": "finplan-shared-financialplanning-project-budget",
                        "BudgetType": "COST",
                        "TimeUnit": {"Ref": "BudgetTimeUnit"},
                        "BudgetLimit": {"Amount": {"Ref": "CostCeilingUsd"}, "Unit": "USD"},
                        "CostFilters": {"TagKeyValue": ["user:project$finplan"]},
                    },
                    "NotificationsWithSubscribers": notifications,
                    "ResourceTags": tag_list("project-budget"),
                },
            },
            "BudgetAlertTopic": {
                "Type": "AWS::SNS::Topic",
                "Properties": {"TopicName": "finplan-shared-financialplanning-budget-alert-topic", "Tags": tag_list("budget-alert-topic")},
            },
            "BudgetEnforcementDenyPolicy": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {"ManagedPolicyName": BUDGET_DENY_POLICY_NAME, "Description": "Attached by the budget action at 100% of the ceiling; only a human removes it.", "PolicyDocument": enforcement_deny_policy()},
            },
            "BudgetActionExecutionRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": "finplan-shared-financialplanning-budget-action-role",
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{"Effect": "Allow", "Principal": {"Service": "budgets.amazonaws.com"}, "Action": "sts:AssumeRole"}],
                    },
                    "PermissionsBoundary": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/" + boundary_name("shared")},
                    "Policies": [
                        {
                            "PolicyName": "apply-budget-deny-policy",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": ["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
                                        "Resource": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/*"},
                                        "Condition": {"ArnEquals": {"iam:PolicyARN": {"Ref": "BudgetEnforcementDenyPolicy"}}},
                                    }
                                ],
                            },
                        }
                    ],
                    "Tags": tag_list("budget-action"),
                },
            },
            "BudgetEnforcementAction": {
                "Type": "AWS::Budgets::BudgetsAction",
                "Properties": {
                    "BudgetName": "finplan-shared-financialplanning-project-budget",
                    "NotificationType": "ACTUAL",
                    "ActionType": "APPLY_IAM_POLICY",
                    "ActionThreshold": {"Type": "PERCENTAGE", "Value": ENFORCEMENT_THRESHOLD_PERCENT},
                    "ApprovalModel": "AUTOMATIC",
                    "ExecutionRoleArn": {"Fn::GetAtt": ["BudgetActionExecutionRole", "Arn"]},
                    "Definition": {
                        "IamActionDefinition": {
                            "PolicyArn": {"Ref": "BudgetEnforcementDenyPolicy"},
                            "Roles": {"Fn::Split": [",", {"Fn::Join": [",", role_lists]}]},
                        }
                    },
                    "Subscribers": [{"Type": "EMAIL", "Address": {"Ref": "NotificationEmail"}}],
                    "ResourceTags": tag_list("budget-action"),
                },
                "DependsOn": ["ProjectBudget"],
            },
        },
        "Outputs": {"BudgetEnforcementDenyPolicyArn": {"Value": {"Ref": "BudgetEnforcementDenyPolicy"}}},
        "Metadata": {
            "finplan": {
                "generated_by": "finplan_contracts.budget",
                "cdk_wiring": "add-platform-foundation",
                "default_allocation_usd": DEFAULT_ALLOCATION,
                "allocation_parameter": ALLOCATION_PARAMETER,
                "state_parameter": STATE_PARAMETER,
                "third_party_spend": "TypeSafe Jev usage is billed from prepaid credits outside AWS and is not counted against the ceiling or any category.",
            }
        },
    }


def generate_templates() -> dict[str, dict[str, Any]]:
    return {
        "budget-alerts-and-action.json": budget_template(),
        "budget-enforcement-deny-policy.json": {
            "Description": "Deny policy attached by the budget action at 100% of the ceiling (ENV-19): new billable compute, model invocations and pipeline executions are denied; reads stay allowed.",
            "PolicyName": "finplan-budget-enforcement-deny",
            "PolicyDocument": enforcement_deny_policy(),
            "Metadata": {"finplan": {"generated_by": "finplan_contracts.budget"}},
        },
    }


# ===================================================================== CLI
def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance budget", description="Budget allocation and pre-flight checks (ENV-17).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("check-allocation", help="validate an allocation map against the ceiling")
    a.add_argument("allocation", nargs="?", help="JSON file (default: the registered default allocation)")
    a.add_argument("--ceiling", type=float, default=None)
    p = sub.add_parser("preflight", help="pre-flight a paid job against its category")
    p.add_argument("--category", required=True)
    p.add_argument("--estimate", type=float, required=True, help="estimated_usd_upper_bound")
    p.add_argument("--allocation", help="allocation JSON file (default: registered defaults)")
    p.add_argument("--spend", help="JSON file with a list of cost records {usd, category, billed_by}")
    p.add_argument("--ceiling", type=float, default=None)
    p.add_argument("--state", help="budget-state JSON file")
    sub.add_parser("defaults", help="print the registered default allocation and ceiling")
    args = ap.parse_args(argv)
    if args.cmd == "defaults":
        print(json.dumps({"cost_ceiling_usd": DEFAULT_CEILING_USD, "allocation": DEFAULT_ALLOCATION}, indent=2))
        return 0
    if args.cmd == "check-allocation":
        alloc = _load_json(args.allocation) if args.allocation else DEFAULT_ALLOCATION
        problems = allocation_problems(alloc, args.ceiling)
        for pr in problems:
            print(f"INVALID: {pr}", file=sys.stderr)
        if not problems:
            print("OK: allocation is valid")
        return 1 if problems else 0
    res = preflight(
        args.category,
        args.estimate,
        _load_json(args.allocation) if args.allocation else None,
        _load_json(args.spend) if args.spend else (),
        ceiling_usd=args.ceiling,
        budget_state=_load_json(args.state) if args.state else None,
    )
    if res.allowed:
        print(f"ALLOW: {args.category} remaining {res.remaining_allocation_usd:g} USD >= estimate {args.estimate:g} USD")
        return 0
    print(json.dumps(res.error, indent=2))
    return 1
