"""Deployment-only beta View-only brokerage exception; canonical contracts stay immutable."""
from finplan_contracts import boundaries,iam

SECRET_NAME='finplan/beta/financeagent/coinbase-brokerage-readonly'
ROLE_NAME='finplan-beta-financeagent-brokerage-read-role'
GET='secretsmanager:GetSecretValue'


def deployment_env_boundary(env,*,partition=boundaries.PARTITION,region=boundaries.REGION,account=boundaries.ACCOUNT):
    doc=boundaries.env_permission_boundary(env,partition=partition,region=region,account=account)
    if env!='beta':return doc
    original=next(s for s in doc['Statement'] if s.get('Sid')=='DenyLiveFinancialSecrets')
    if GET not in original['Action']:raise ValueError('canonical financial secret deny changed; review the deployment overlay')
    patterns=original['Condition']['StringLike']['secretsmanager:SecretId']
    original['Action']=[a for a in original['Action'] if a!=GET]
    role=f'arn:{partition}:iam::{account}:role/{ROLE_NAME}'
    secret=f'arn:{partition}:secretsmanager:{region}:{account}:secret:{SECRET_NAME}-??????'
    additions=[
        # Preserve the base financial Get denial for every other principal.
        {'Effect':'Deny','Action':GET,'Resource':'*','Condition':{'StringLike':{'secretsmanager:SecretId':patterns},'ArnNotEquals':{'aws:PrincipalArn':role}}},
        # Even a broad identity grant cannot read another secret through this role.
        {'Effect':'Deny','Action':GET,'NotResource':secret,'Condition':{'ArnEquals':{'aws:PrincipalArn':role}}},
        # Protect the target even when the request lacks SecretId condition context.
        {'Effect':'Deny','Action':GET,'Resource':secret,'Condition':{'ArnNotEquals':{'aws:PrincipalArn':role}}},
        # No mutation, extra metadata operation, or other action on the target.
        {'Effect':'Deny','NotAction':GET,'Resource':secret},
        # Both the actual resource and caller's name/ARN identifier must match.
        {'Effect':'Deny','Action':GET,'Resource':'*','Condition':{'ArnEquals':{'aws:PrincipalArn':role},'StringNotLike':{'secretsmanager:SecretId':[SECRET_NAME,secret]}}},
        # BatchGet uses Resource '*'; reject it for this role regardless of context.
        {'Effect':'Deny','Action':'secretsmanager:BatchGetSecretValue','Resource':'*','Condition':{'ArnEquals':{'aws:PrincipalArn':role}}},
    ]
    doc['Statement'].extend(iam.to_cfn(additions))
    # Sid is optional audit metadata. Omitting it only in this deployed beta policy
    # preserves enforcement while keeping the resolved managed policy under 6144.
    for statement in doc['Statement']:statement.pop('Sid',None)
    return doc
