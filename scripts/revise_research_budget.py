"""Revise the explicitly authorized project budget through its CloudFormation owner.

Default is read-only. --apply updates the ceiling, re-resolves the existing stack's
SSM parameter, then publishes allocations. Does not reset research consumption.
"""
from __future__ import annotations

import argparse
import json

CEILING = "/finplan/shared/financialplanning/config/cost-ceiling-usd"
ALLOCATION = "/finplan/shared/financialplanning/config/budget-allocation"
STACK = "finplan-shared-financialplanning-tooling"
ALLOCATIONS = {"platform_infra": 16, "cpu_research": 25, "bedrock_explanations": 10, "gpu": 25, "reserve": 24}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--finalize", action="store_true", help="publish allocations after stack and consumer verification")
    args = ap.parse_args()
    import boto3
    s = boto3.Session(region_name="us-east-2")
    ssm, cf = s.client("ssm"), s.client("cloudformation")
    old = float(ssm.get_parameter(Name=CEILING)["Parameter"]["Value"])
    allocation = json.loads(ssm.get_parameter(Name=ALLOCATION)["Parameter"]["Value"])
    assert sum(ALLOCATIONS.values()) == 100
    print(json.dumps({"previous_ceiling": old, "new_ceiling": 100, "previous_allocation": allocation,
                      "new_allocation": ALLOCATIONS, "apply": args.apply}))
    if args.finalize:
        stack = cf.describe_stacks(StackName=STACK)["Stacks"][0]
        if stack["StackStatus"] != "UPDATE_COMPLETE" or old != 100:
            raise RuntimeError("Budget stack is not successfully revised")
        account = s.client("sts").get_caller_identity()["Account"]
        budget = s.client("budgets", region_name="us-east-1").describe_budget(
            AccountId=account, BudgetName="finplan-shared-financialplanning-project-budget")["Budget"]
        if float(budget["BudgetLimit"]["Amount"]) != 100:
            raise RuntimeError("AWS budget does not match the authorized ceiling")
        ssm.put_parameter(Name=ALLOCATION, Value=json.dumps(ALLOCATIONS, sort_keys=True), Type="String", Overwrite=True)
        print("Authorized allocations published; all previous job spend remains counted")
        return
    if not args.apply:
        return
    if old not in (50., 100.):
        raise RuntimeError("Unexpected live ceiling; refusing to overwrite a different authorization")
    stack = cf.describe_stacks(StackName=STACK)["Stacks"][0]
    ssm.put_parameter(Name=CEILING, Value="100", Type="String", Overwrite=True)
    try:
        cf.update_stack(StackName=STACK, UsePreviousTemplate=True,
                        Parameters=[{"ParameterKey": p["ParameterKey"], "UsePreviousValue": True} for p in stack.get("Parameters", [])],
                        Capabilities=["CAPABILITY_NAMED_IAM"])
        print("CloudFormation budget revision started; allocations publish after successful completion")
    except cf.exceptions.ClientError as exc:
        if "No updates are to be performed" not in str(exc):
            ssm.put_parameter(Name=CEILING, Value=str(int(old)), Type="String", Overwrite=True)
            raise
    # The caller polls stack completion separately; do not publish a mismatched allocation.


if __name__ == "__main__":
    main()
