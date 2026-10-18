"""Exact beta managed inference reads isolated books; no new write/data-plane grants."""
import copy
import hashlib
import json

import pytest

from finplan_platform.core.clock import to_timestamp
from finplan_platform.core.modeling_evidence import ModelingEvidence
from finplan_platform.handlers.api import ApiApp, LocalClient, BETA_MANAGED_READER_ROLE, beta_managed_read, ROUTES, route_allowed
from finplan_platform.core.config import load_config
from infra.stacks.api import resource_policy_document
from infra.policy_sim import Principal, role_arn, simulate
from tests.api_support import *

WORST = {'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'execute-api:*','Resource':'*'}]}
IDS = {'portfolio_id':'pf_01KDVDNAZ83BAMMYCEGWF33DPM','decision_id':'pd_01KDVDNAZ83BAMMYCEGWF33DPM','revision':'1','plan_id':'pl_01KDVDNAZ83BAMMYCEGWF33DPM','plan_version_id':'pv_01KDVDNAZ83BAMMYCEGWF33DPM','publication_id':'pub_01KDVDNAZ83BAMMYCEGWF33DPM','run_id':'run_01KDVDNAZ83BAMMYCEGWF33DPM','execution_id':'exe_01KDVDNAZ83BAMMYCEGWF33DPM','input_snapshot_id':'snap_01KDVDNAZ83BAMMYCEGWF33DPM','import_id':'imp_01KDVDNAZ83BAMMYCEGWF33DPM','session_date':'2026-01-12'}


@pytest.mark.parametrize('session', [False,True])
def test_application_exact_job_reads_original_isolated_metadata_and_state(svc, app, clock, session):
    evidence=ModelingEvidence(svc.repo,svc.artifacts)
    book=evidence.ensure_portfolio('controlled-managed-read','jev_systemone',100000.,['SPY'],to_timestamp(clock.now()))
    record=svc.repo.require('portfolio',book['portfolio_id'])
    before=(copy.deepcopy(record.doc),copy.deepcopy(record.attrs))
    principal=role(BETA_MANAGED_READER_ROLE) if not session else 'arn:aws:sts::<account-id>:assumed-role/'+BETA_MANAGED_READER_ROLE+'/controlled-managed-session'
    client=LocalClient(app,principal)
    path='/v1/portfolios/'+book['portfolio_id']
    status,metadata,_=client.get(path)
    assert status==200 and metadata['settings']['isolated_experiment'] is True
    assert metadata['settings']['experiment_id']=='controlled-managed-read'
    status,state,_=client.get(path+'/state')
    assert status==200 and state['paper_state']==book['paper_state'] and state['revision']==book['revision']
    after=svc.repo.require('portfolio',book['portfolio_id'])
    assert (after.doc,after.attrs)==before
    assert evidence.get_portfolio(book['portfolio_id'])==book


def test_ordinary_user_synthetic_book_is_still_denied_to_managed_job(clients):
    status,book,_=clients.operator.post('/v1/portfolios',{'name':'Existing user paper book','base_currency':'USD','synthetic':True,'settings':{'paper_research_profile':{'instruments':['SPY']}},'idempotency_key':'ordinary-managed-read-negative'})
    assert status==201
    for suffix in ('','/state','/history','/decisions'):
        status,error,_=clients.fm_job.get('/v1/portfolios/'+book['portfolio_id']+suffix)
        assert status==403 and error['code']=='FORBIDDEN'
    assert clients.operator.get('/v1/portfolios/'+book['portfolio_id'])[0]==200


@pytest.mark.parametrize('name',['finplan-beta-financemodel-job-execution-role-extra','finplan-gamma-financemodel-job-execution-role','finplan-prod-financemodel-job-execution-role'])
def test_other_principals_and_every_write_remain_denied(svc,app,clock,name):
    book=ModelingEvidence(svc.repo,svc.artifacts).ensure_portfolio('controlled-managed-read','jev_systemone',100000.,['SPY'],to_timestamp(clock.now()))
    client=LocalClient(app,role(name));path='/v1/portfolios/'+book['portfolio_id']
    assert client.get(path)[0]==403 and client.get(path+'/state')[0]==403
    for method,suffix in [('PUT','/state'),('POST','/decisions')]:
        assert LocalClient(app,role(BETA_MANAGED_READER_ROLE)).call(method,path+suffix,{})[0]==403


@pytest.mark.parametrize('env',['beta','gamma','prod'])
def test_resource_policy_only_adds_two_reads_for_exact_beta_managed_role(env):
    policy=resource_policy_document(load_config(env),role_arn)
    principal=Principal.role(f'finplan-{env}-financemodel-job-execution-role',WORST)
    for route in ROUTES:
        resource='execute-api:/live/'+route.method+route.path.format(**IDS)
        actual=simulate('execute-api:Invoke',resource,principal,resource_policy=policy).allowed
        expected=route_allowed(route,'financemodel-job') or beta_managed_read(route,env,f'finplan-{env}-financemodel-job-execution-role')
        if env!='beta' and route.operation in __import__('finplan_platform.handlers.api',fromlist=['BETA_LIFECYCLE_OPERATIONS']).BETA_LIFECYCLE_OPERATIONS:continue
        assert actual==expected,(env,route.path,actual,expected)
    assert len(json.dumps(policy).replace('<account-id>','0'*12).replace('<region>','us-east-2').encode())<=8192-512


def test_other_managed_role_keeps_snapshot_reads_without_portfolio_expansion():
    policy=resource_policy_document(load_config('beta'),role_arn)
    principal=Principal.role(BETA_MANAGED_READER_ROLE+'-extra',WORST)
    for path in ('/v1/portfolios/'+IDS['portfolio_id'],'/v1/portfolios/'+IDS['portfolio_id']+'/state'):
        assert not simulate('execute-api:Invoke','execute-api:/live/GET'+path,principal,resource_policy=policy).allowed
    assert simulate('execute-api:Invoke','execute-api:/live/GET/v1/snapshots/'+IDS['input_snapshot_id'],principal,resource_policy=policy).allowed


@pytest.mark.parametrize('cls,name',[('reader','reader'),('submitter','submitter'),('plan-writer','plan-writer')])
def test_factored_tool_denials_preserve_every_existing_route_and_unknown_route(cls,name):
    policy=resource_policy_document(load_config('beta'),role_arn)
    principal=Principal.role('finplan-beta-financelambdastool-tool-role-'+name,WORST)
    for route in ROUTES:
        actual=simulate('execute-api:Invoke','execute-api:/live/'+route.method+route.path.format(**IDS),principal,resource_policy=policy).allowed
        assert actual==route_allowed(route,cls),(cls,route.method,route.path)
    for method in ('GET','POST','PUT','DELETE'):
        assert not simulate('execute-api:Invoke',f'execute-api:/live/{method}/v1/unknown-future-operation',principal,resource_policy=policy).allowed


@pytest.mark.parametrize('env,expected', [
    ('gamma','cadde4d8e8c3a57362d21ad5eb9c5d64f6a7af051ce619461dc1fdaf7b3bb1db'),
    ('prod','a255c3e5f2b2e5d9bfc73101afe73a6dd868dc5522563d7b514e34263215d6eb')])
def test_higher_policy_documents_equal_prechange_e014_baseline(env,expected):
    # Hashes independently obtained by executing the original e014 pure builder;
    # new beta reads must not change even policy serialization above beta.
    policy=resource_policy_document(load_config(env),role_arn)
    assert hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(',',':')).encode()).hexdigest()==expected
