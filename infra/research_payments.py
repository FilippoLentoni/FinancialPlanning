"""Separate beta research-payment boundary, never used by the four paper pipelines."""
from __future__ import annotations

MANAGER_NAME = "finplanbetaresearch"
BOUNDARY_NAME = "finplan-beta-research-payment-permission-boundary"
STACK_NAME = "finplan-beta-financialplanning-research-payments"
MANAGEMENT_ACTIONS = ["bedrock-agentcore:" + name for name in (
    "CreatePaymentInstrument", "GetPaymentInstrument", "ListPaymentInstruments",
    "DeletePaymentInstrument", "GetPaymentInstrumentBalance", "CreatePaymentSession",
    "GetPaymentSession", "ListPaymentSessions", "DeletePaymentSession")]
EXECUTION_ACTIONS = ["bedrock-agentcore:" + name for name in (
    "ProcessPayment", "GetPaymentInstrument", "GetPaymentInstrumentBalance", "GetPaymentSession")]


def manager_arn(account, region="us-east-2", partition="aws"):
    return f"arn:{partition}:bedrock-agentcore:{region}:{account}:payment-manager/{MANAGER_NAME}-*"


def boundary(account, region="us-east-2", partition="aws"):
    base = f"arn:{partition}:bedrock-agentcore:{region}:{account}:"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "OwnedPaymentManagerOnly", "Effect": "Allow",
         "Action": sorted(set(MANAGEMENT_ACTIONS + EXECUTION_ACTIONS)), "Resource": manager_arn(account, region, partition)},
        {"Sid": "ServiceIdentity", "Effect": "Allow", "Action": [
            "bedrock-agentcore:CreateWorkloadIdentity", "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetResourcePaymentToken", "bedrock-agentcore:TagResource"],
         "Resource": [base + "workload-identity-directory/default", base + f"workload-identity-directory/default/workload-identity/{MANAGER_NAME}-*", base + "token-vault/default"]},
        {"Sid": "TaggedIdentityCreation", "Effect": "Allow", "Action": ["bedrock-agentcore:CreateWorkloadIdentity", "bedrock-agentcore:TagResource"],
         "Resource": [base + "workload-identity-directory/default", base + "workload-identity-directory/default/workload-identity/*"],
         "Condition": {"StringEquals": {"aws:RequestTag/project": "finplan", "aws:RequestTag/owner-repo": "financeagent", "aws:RequestTag/environment": "beta"}}},
        {"Sid": "ProviderProvisioning", "Effect": "Allow", "Action": [
            "bedrock-agentcore:CreatePaymentCredentialProvider", "bedrock-agentcore:GetPaymentCredentialProvider",
            "bedrock-agentcore:GetResourcePaymentToken", "bedrock-agentcore:TagResource"],
         "Resource": [base + "token-vault/default", base + "token-vault/default/paymentcredentialprovider/*"]},
        # AgentCore appends an exact per-connector secret grant to its service role.
        # This upper bound grants no secret access by itself; execution/management
        # identity policies contain no Secrets Manager permission.
        {"Sid": "ServiceCredentialUpperBound", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
         "Resource": f"arn:{partition}:secretsmanager:{region}:{account}:secret:*",
         "Condition": {"StringEquals": {"aws:ResourceAccount": account}}},
    ]}


def template():
    doc = boundary("${AWS::AccountId}", "${AWS::Region}", "${AWS::Partition}")
    # Resolve strings recursively; keep the exported template account agnostic.
    def sub(value):
        if isinstance(value, str) and "${AWS::" in value: return {"Fn::Sub": value}
        if isinstance(value, dict): return {k: sub(v) for k, v in value.items()}
        if isinstance(value, list): return [sub(v) for v in value]
        return value
    return {"AWSTemplateFormatVersion": "2010-09-09", "Description": "Independent beta research-payment boundary; no changes to paper-planning boundaries",
            "Resources": {"ResearchPaymentBoundary": {"Type": "AWS::IAM::ManagedPolicy", "Properties": {
                "ManagedPolicyName": BOUNDARY_NAME, "Description": "Separate owned research-payment upper bound", "PolicyDocument": sub(doc)}}},
            "Outputs": {"BoundaryArn": {"Value": {"Ref": "ResearchPaymentBoundary"}}}}
