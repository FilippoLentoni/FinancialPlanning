"""Original producer-controlled worker receipts, never real GPU/forward results."""
import base64,copy,csv,io,json
from pathlib import Path
import pytest
from finplan_platform.reporting.managed_evidence import collect
from finplan_platform.reporting.reports import EvidenceError,captured_input_bytes,canonical,checksum,aggregate,write_report,csv_export
from finplan_platform.reporting.transport import encode_transport,decode_transport
from finplan_platform.reporting import refresh

FIXTURES=Path(__file__).resolve().parents[2]/'fixtures'/'reporting'/'managed-inference'
def fixture(name='success'):
    return json.loads((FIXTURES/('qwen-'+name+'.json')).read_bytes())
def observation(event,oid='controlled-worker-observation'):
    return {'observation_id':oid,'kind':'managed_inference_evidence:'+event['managed_run_id'],'slot':event['occurred_at'],'observed_at':'2026-07-12T15:00:00Z','checksum':checksum(canonical(event)),**{k:event[k]for k in('experiment_id','approach_id','portfolio_id')},'payload':event}
def report(event):
    return {'experiment_id':event['experiment_id'],'registry':[{'approach_id':event['approach_id'],'portfolio_id':event['portfolio_id']}],'generated_at':'2026-07-13T16:00:00Z','artifact_chunks':{},'additional_inputs':{},'inputs':{},'runs':[],'observations':[],'missing_slots':[],'ancillary_evidence':[observation(event)]}

@pytest.mark.parametrize('name,count',[('success',12),('failed',7)])
def test_original_producer_bytes_failed_roles_and_context_survive_transport(name,count):
    event=fixture(name)['inference_evidence'];data=collect(report(event));model=data['managed_inference_events'][0]
    assert len(model['artifact_summaries'])==count and model['non_trading']and not model['primary_trading_opportunity']
    assert model['model_success']is(name=='success')
    assert data['runs']==[] and data['observations']==[]
    transported=decode_transport(encode_transport(data))
    for original,layer in zip(event['artifacts'],transported['additional_inputs'][event['event_id']]):
        assert captured_input_bytes(transported,layer)==base64.b64decode(original['raw_base64'])
    # Model/input source is original request, never a replacement current book.
    bindings=[a for a in model['artifact_summaries']if a['consumption_status']=='binding_not_model_input'];assert len(bindings)==1 and bindings[0]['captured_at']is None
    roles=[a for a in model['artifact_summaries']if a['kind']=='role_input']
    if name=='failed':assert any(a['consumption_status']=='prepared_input_without_confirmed_response'for a in roles)
    else:assert all(a['consumption_status']=='input_with_original_response_receipt'for a in roles)
    rows=list(csv.DictReader(io.StringIO(csv_export(data))));assert len(rows)==1 and rows[0]['row_kind']=='managed_worker_nontrading'and rows[0]['fill_status']=='nontrading_managed_inference_only'
    assert rows[0]['run_id']==event['event_id']and rows[0]['origin_run_id']==event['origin_run_id']

@pytest.mark.parametrize('change',[lambda e:e['artifacts'][1].update(raw_base64=base64.b64encode(b'{}').decode()),lambda e:e.update(origin_input_id='today-replacement'),lambda e:e.update(portfolio_id='other-book')])
def test_wrong_bytes_reconstructed_origin_or_other_book_rejected(change):
    event=fixture()['inference_evidence'];data=report(event);data['ancillary_evidence'][0]['payload']=copy.deepcopy(event);change(data['ancillary_evidence'][0]['payload'])
    with pytest.raises(EvidenceError):collect(data)

def test_identical_duplicate_worker_event_not_extra_decision_or_cost():
    event=fixture()['inference_evidence'];data=report(event);data['ancillary_evidence'].append(observation(copy.deepcopy(event),'second-source'))
    collect(data);assert len(data['managed_inference_events'])==1 and len(data['managed_inference_events'][0]['evidence_sources'])==2
    assert data['runs']==[] and 'managed_cost_events'not in data


def test_strong_gsi_lag_receipts_cost_and_original_worker_archive_before_ack(repo,artifacts,tmp_path,monkeypatch):
    from finplan_platform.core.modeling_evidence import ModelingEvidence
    from finplan_platform.core.repository import PutNew
    producer=fixture('failed');event=producer['inference_evidence'];cost=producer['cost_event'];backend=ModelingEvidence(repo,artifacts)
    seed=backend.ensure_portfolio(event['experiment_id'],event['approach_id'],1000,['SPY'],'2026-07-11T00:00:00Z')
    source=repo.require('portfolio',seed['portfolio_id'])
    # Controlled original producer uses its own fixed synthetic book ID. Seed the
    # same book context in the real repository without rewriting any worker bytes.
    repo.commit([PutNew('portfolio',doc={**source.doc,'portfolio_id':event['portfolio_id']},extra_attrs={'paper_state':source.attrs['paper_state'],'paper_state_revision':1},contract_version='1.13.1')],audit=[])
    registry=[{'approach_id':event['approach_id'],'portfolio_id':event['portfolio_id'],'family':'language_model','cohort_id':'controlled','comparison_policy_id':'controlled'}]
    backend.register_approaches(event['experiment_id'],registry,{},'2026-07-11T00:00:00Z')
    queue=tmp_path/'queue';tasks=[]
    for payload,kind in [(cost,'managed_model_cost:'),(event,'managed_inference_evidence:')]:
        receipt=backend.record_observation(event['experiment_id'],event['approach_id'],payload['occurred_at'],event['portfolio_id'],payload,'2026-07-12T15:00:00Z',kind=kind+event['managed_run_id'])
        task={'protocol':refresh.DAILY,'experiment_id':event['experiment_id'],'reporting_day':'2026-07-11','event_id':payload['event_id'],'observation_id':receipt['observation_id'],'checksum':receipt['checksum']};refresh.put_task(queue,task)
    tasks,_=refresh.read_tasks(queue,[event['experiment_id']]);receipts=refresh.verify_receipts(backend,tasks)
    original=repo.query_index
    monkeypatch.setattr(repo,'query_index',lambda table,*a,**kw:([],None)if table=='portfolio_observation'else original(table,*a,**kw))
    data=aggregate(backend,experiment_id=event['experiment_id'],registry=registry,start_day='2026-07-11',generated_at='2026-07-13T16:00:00Z',mode='controlled_replay',required_observations=receipts)
    refresh.ensure_in_report(data,tasks)
    assert len(data['managed_inference_events'])==1 and len(data['managed_cost_events'])==1
    assert data['runs']==[]and data['observations']==[]and backend.get_portfolio(event['portfolio_id'])['revision']==1
    written=write_report(data,tmp_path/'reports',update_latest=False)
    assert '2026-07-11'in written['aliases_published']
    exported=decode_transport(json.loads((Path(written['directory'])/'2026-07-11.json').read_bytes()))
    for original,layer in zip(event['artifacts'],exported['additional_inputs'][event['event_id']]):assert captured_input_bytes(exported,layer)==base64.b64decode(original['raw_base64'])
    refresh.acknowledge(tasks);assert not list(queue.glob('*.json'))


def test_role_response_cannot_be_attributed_to_a_different_actual_request():
    event=fixture()['inference_evidence']
    original=event['artifacts'][2];payload=json.loads(base64.b64decode(original['raw_base64']));payload['messages_checksum']='sha256:'+'0'*64
    raw=canonical(payload);original.update(raw_base64=base64.b64encode(raw).decode(),checksum=checksum(raw),size_bytes=len(raw));original['ref'].update(checksum=checksum(raw),size_bytes=len(raw))
    with pytest.raises(EvidenceError,match='response/input association'):collect(report(event))

def test_inference_cost_link_cannot_point_to_replaced_event():
    producer=fixture();event=producer['inference_evidence'];data=report(event)
    cost=producer['cost_event'];cost['job_state']='replaced'
    data['ancillary_evidence'].append({'kind':'managed_model_cost:'+event['managed_run_id'],'payload':cost})
    with pytest.raises(EvidenceError,match='cost event link'):collect(data)


def test_managed_frontend_bytes_and_full_renderer_source_checksum_are_embedded():
    import re
    from finplan_platform.reporting.reports import html_export
    import finplan_platform.reporting.reports as implementation
    root=Path(implementation.__file__).parent
    data=collect(report(fixture('failed')['inference_evidence']))
    html=html_export(data)
    embedded=[base64.b64decode(value) for value in re.findall(r"atob\('([A-Za-z0-9+/=]+)'\)",html)]
    for filename in ('report.js','managed_evidence.js','page_codec.js'):
        path=root/filename
        if path.exists():assert any(path.read_bytes()in source for source in embedded),filename
    assert 'managedWorkerExplorer'in (root/'managed_evidence.js').read_text()


@pytest.mark.parametrize('field',['worker_image_digest','worker_source_commit','worker_code_checksum','approval'])
def test_same_binding_worker_input_cannot_use_other_approved_worker(field):
    event=fixture()['inference_evidence'];original=event['artifacts'][1];payload=json.loads(base64.b64decode(original['raw_base64']))
    payload[field]={'approved_by':'other-approver'}if field=='approval'else'other-worker-pin'
    raw=canonical(payload);original.update(raw_base64=base64.b64encode(raw).decode(),checksum=checksum(raw),size_bytes=len(raw));original['ref'].update(checksum=checksum(raw),size_bytes=len(raw))
    with pytest.raises(EvidenceError,match='worker input'):collect(report(event))
