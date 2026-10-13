from finplan_contracts.boundaries import env_permission_boundary
from finplan_contracts.iam import Request, evaluate
from infra.research_payments import boundary, manager_arn, template


def test_payment_boundary_scopes_manager_and_keeps_original_denial():
    account = "<account-id>"
    owned = manager_arn(account).replace("*", "abc123")
    identity = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "bedrock-agentcore:ProcessPayment", "Resource": "*"}]}
    assert evaluate(Request("bedrock-agentcore:ProcessPayment", owned), [identity], boundary(account)).allowed
    assert not evaluate(Request("bedrock-agentcore:ProcessPayment", owned.replace("finplanbetaresearch", "other")), [identity], boundary(account)).allowed
    assert not evaluate(Request("bedrock-agentcore:ProcessPayment", owned.replace("us-east-2", "us-east-1")), [identity], boundary(account)).allowed
    assert not evaluate(Request("bedrock-agentcore:ProcessPayment", owned), [identity], env_permission_boundary("beta", account=account, region="us-east-2", partition="aws")).allowed


def test_boundary_cannot_trade_or_modify_iam_and_template_is_independent():
    identity = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
    for action in ["coinbase:PlaceOrder", "iam:PutRolePolicy", "iam:PassRole", "bedrock-agentcore:CreatePaymentManager"]:
        assert not evaluate(Request(action), [identity], boundary("<account-id>")).allowed
    doc = template()
    assert list(doc["Resources"]) == ["ResearchPaymentBoundary"]


def test_create_time_identity_grant_requires_fixed_project_tags():
    account = "<account-id>"
    resource = f"arn:aws:bedrock-agentcore:us-east-2:{account}:workload-identity-directory/default/workload-identity/*"
    identity = {"Statement": [{"Effect": "Allow", "Action": "bedrock-agentcore:TagResource", "Resource": "*"}]}
    context = {"aws:RequestTag/project": "finplan", "aws:RequestTag/owner-repo": "financeagent", "aws:RequestTag/environment": "beta"}
    assert evaluate(Request("bedrock-agentcore:TagResource", resource, context), [identity], boundary(account)).allowed
    assert not evaluate(Request("bedrock-agentcore:TagResource", resource), [identity], boundary(account)).allowed
    context["aws:RequestTag/environment"] = "prod"
    assert not evaluate(Request("bedrock-agentcore:TagResource", resource, context), [identity], boundary(account)).allowed
