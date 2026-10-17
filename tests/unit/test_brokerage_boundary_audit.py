"""Deployment audit must never suppress a changed policy or an Allow grant."""
from copy import deepcopy

import pytest
from finplan_contracts import boundaries, live_perms
from infra.stacks.brokerage_boundary import audited_deny_reference_locations, deployment_env_boundary


def template():
    return {'Resources': {'BetaPermissionBoundary': {
        'Type': 'AWS::IAM::ManagedPolicy', 'Properties': {
            'ManagedPolicyName': boundaries.boundary_name('beta'),
            'PolicyDocument': deployment_env_boundary('beta'),
        }}}}


def remaining(doc):
    audit=audited_deny_reference_locations(doc)
    return [f for f in live_perms.scan_document(doc) if not (
        f.rule=='live-secret' and f.location in audit)]


def test_exact_overlay_only_audits_four_deny_leaves():
    doc=template()
    assert len(live_perms.scan_document(doc))==4
    assert len(audited_deny_reference_locations(doc))==4
    assert remaining(doc)==[]


@pytest.mark.parametrize('mutation', ['allow','secret','role','extra_statement','extra_resource','gamma'])
def test_policy_mutations_and_extra_resource_still_fail(mutation):
    doc=deepcopy(template())
    props=doc['Resources']['BetaPermissionBoundary']['Properties']
    stmts=props['PolicyDocument']['Statement']
    if mutation=='allow': stmts[13]['Effect']='Allow'
    elif mutation=='secret': stmts[13]['Resource']={'Fn::Sub':'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:other-coinbase-??????'}
    elif mutation=='role': stmts[13]['Condition']['ArnNotEquals']['aws:PrincipalArn']={'Fn::Sub':'arn:${AWS::Partition}:iam::${AWS::AccountId}:role/other'}
    elif mutation=='extra_statement': stmts.append({'Effect':'Allow','Action':'secretsmanager:GetSecretValue','Resource':'*'})
    elif mutation=='extra_resource': doc['Resources']['Rogue']={'Type':'AWS::IAM::Policy','Properties':{'PolicyDocument':{'Statement':[{'Effect':'Allow','Action':'secretsmanager:GetSecretValue','Resource':':'.join(['arn','aws','secretsmanager','us-east-2','test','secret','coinbase-rogue'])}]}}}
    else: props['ManagedPolicyName']=boundaries.boundary_name('gamma')
    assert remaining(doc)
    if mutation!='extra_resource': assert not audited_deny_reference_locations(doc)
