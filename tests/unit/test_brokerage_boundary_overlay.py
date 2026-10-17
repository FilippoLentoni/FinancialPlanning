"""Worst-case identity policy cannot widen the deployment's single View-only exception."""
import json
import pytest
from finplan_contracts import boundaries
from finplan_contracts.iam import Request,evaluate
from infra.stacks.brokerage_boundary import deployment_env_boundary,SECRET_NAME,ROLE_NAME,GET

ACCOUNT='1'*12
ROLE=f'arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}'
SECRET=f'arn:aws:secretsmanager:us-east-2:{ACCOUNT}:secret:{SECRET_NAME}-aB123x'
WORST={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'*','Resource':'*'}]}


def policy(env='beta'):
    return deployment_env_boundary(env,partition='aws',region='us-east-2',account=ACCOUNT)


def allowed(action=GET,resource=SECRET,*,principal=ROLE,identifier=SECRET_NAME,env='beta',include_identifier=True):
    context={'aws:PrincipalArn':principal,'aws:PrincipalTag/environment':env,'aws:ResourceTag/environment':env}
    if include_identifier:context['secretsmanager:SecretId']=identifier
    return evaluate(Request(action,resource,context),[WORST],policy(env)).allowed


@pytest.mark.parametrize('identifier',[SECRET_NAME,SECRET])
def test_only_exact_reader_and_secret_name_or_full_arn_are_allowed(identifier):
    assert allowed(identifier=identifier)
    canonical=boundaries.env_permission_boundary('beta',partition='aws',region='us-east-2',account=ACCOUNT)
    context={'aws:PrincipalArn':ROLE,'aws:PrincipalTag/environment':'beta','aws:ResourceTag/environment':'beta','secretsmanager:SecretId':identifier}
    assert not evaluate(Request(GET,SECRET,context),[WORST],canonical).allowed


@pytest.mark.parametrize('overrides',[
    {'principal':ROLE+'-other'},
    {'principal':ROLE.replace('beta','gamma')},
    {'principal':ROLE.replace(ACCOUNT,'2'*12)},
    {'identifier':SECRET_NAME+'-other'},
    {'identifier':SECRET+'extra'},
    {'identifier':SECRET.rsplit('-',1)[0]},
    {'resource':SECRET+'extra'},
    {'resource':SECRET.replace('aB123x','aB123')},
    {'resource':SECRET.replace('us-east-2','us-east-1')},
    {'resource':SECRET.replace(ACCOUNT,'2'*12)},
    {'resource':SECRET.replace('beta','gamma')},
    {'include_identifier':False},
    {'env':'gamma'}, {'env':'prod'},
])
def test_exception_rejects_wrong_role_resource_identifier_or_environment(overrides):
    assert not allowed(**overrides)


@pytest.mark.parametrize('action',[
    'secretsmanager:PutSecretValue','secretsmanager:UpdateSecret','secretsmanager:DeleteSecret',
    'secretsmanager:DescribeSecret','secretsmanager:RotateSecret','secretsmanager:TagResource',
    'secretsmanager:BatchGetSecretValue','bedrock-agentcore:ProcessPayment','brokerage:SubmitOrder',
])
def test_no_mutation_metadata_batch_or_financial_action_is_granted(action):
    assert not allowed(action)


def test_batch_resource_star_and_other_secret_reads_cannot_escape_reader_scope():
    assert not allowed('secretsmanager:BatchGetSecretValue','*',include_identifier=False)
    name='finplan/beta/financialplanning/ordinary-client'
    arn=f'arn:aws:secretsmanager:us-east-2:{ACCOUNT}:secret:{name}-aB123x'
    assert not allowed(resource=arn,identifier=name)
    other=ROLE.replace('brokerage-read-role','other-role')
    assert allowed(resource=arn,identifier=name,principal=other)
    # Preserve ordinary roles' pre-existing nonfinancial BatchGet behavior.
    assert allowed('secretsmanager:BatchGetSecretValue','*',identifier=name,principal=other)
    financial='finplan/beta/financeagent/coinbase-live'
    assert not allowed(resource=arn.replace(name,financial),identifier=financial,principal=other)


def test_other_boundaries_and_canonical_package_are_unchanged_and_beta_fits_iam_limit():
    before=boundaries.env_permission_boundary('beta')
    default=deployment_env_boundary('beta')
    assert boundaries.env_permission_boundary('beta')==before
    for env in ('gamma','prod'):
        assert json.dumps(deployment_env_boundary(env),sort_keys=True)==json.dumps(boundaries.env_permission_boundary(env),sort_keys=True)
    resolved=boundaries.concrete_boundary(default,partition='aws',region='us-east-2',account=ACCOUNT)
    assert len(json.dumps(resolved,separators=(',',':'))) < 6144
    # The normal envelope generator is untouched; tooling uses the overlay only
    # for its beta environment boundary, not research or shared boundaries.
    assert policy()!=boundaries.env_permission_boundary('beta',partition='aws',region='us-east-2',account=ACCOUNT)
