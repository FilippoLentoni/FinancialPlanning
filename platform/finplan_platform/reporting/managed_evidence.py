"""Independent immutable managed-worker evidence; never a second paper trade."""
from __future__ import annotations
import base64
import json
from finplan_contracts.canonical import canonicalize
from .reports import EvidenceError, checksum, canonical, timestamp, _safe_content, _portable_capture

PROTOCOL='finplan-managed-inference-evidence/1'
MAX_ARTIFACTS=32
MAX_BYTES=8*1024*1024


def is_managed_inference(kind):
    return str(kind).startswith('managed_inference_evidence:')


def collect(report):
    known={a['approach_id']:a for a in report['registry']};events={}
    for row in report.get('ancillary_evidence',[]):
        if not is_managed_inference(row.get('kind')):continue
        event=row.get('payload')
        if not isinstance(event,dict) or event.get('protocol')!=PROTOCOL:
            raise EvidenceError('Managed worker evidence protocol/payload missing')
        job=event.get('managed_run_id');event_id='managed_inputs:'+str(job)
        if not isinstance(job,str)or not job or event.get('event_id')!=event_id or row.get('kind')!='managed_inference_evidence:'+job or event.get('cost_event_id')!='managed:'+job:
            raise EvidenceError('Managed worker permanent event identity mismatch')
        for field in ('experiment_id','approach_id','portfolio_id'):
            if row.get(field)!=event.get(field):raise EvidenceError('Managed worker observation/event identity mismatch')
        approach=known.get(event['approach_id'])
        if not approach or approach['portfolio_id']!=event['portfolio_id'] or event['experiment_id']!=report['experiment_id']:
            raise EvidenceError('Managed worker evidence belongs to another experiment/book')
        if timestamp(event['occurred_at'])!=timestamp(row['slot']):raise EvidenceError('Managed worker event actual occurrence differs from observation slot')
        if timestamp(row['observed_at'])>timestamp(report['generated_at']):raise EvidenceError('Managed worker receipt follows report cutoff')
        for cost in report.get('ancillary_evidence',[]):
            if str(cost.get('kind','')).startswith('managed_model_cost:') and (cost.get('payload')or{}).get('event_id')==event['cost_event_id']:
                if cost.get('checksum')!=event.get('cost_event_checksum'):
                    raise EvidenceError('Managed original inference/cost event link checksum mismatch')
        prior=events.get(event_id)
        if prior:
            if canonical(prior['_original'])!=canonical(event):raise EvidenceError('Conflicting permanent managed worker evidence')
            prior['evidence_sources'].append({'observation_id':row['observation_id'],'checksum':row['checksum']});continue
        artifacts=event.get('artifacts')
        if not isinstance(artifacts,list)or len(artifacts)>MAX_ARTIFACTS:raise EvidenceError('Managed worker original artifact count exceeds bound')
        decoded=[];total=0;identities={}
        for original in artifacts:
            ref=original.get('ref')or {};declared=original.get('size_bytes');encoded=original.get('raw_base64')
            if ref.get('owner')!='financemodel'or ref.get('kind')!='run_artifact' or not isinstance(ref.get('artifact_id'),str):
                raise EvidenceError('Managed worker original artifact owner/kind mismatch')
            if isinstance(declared,bool)or not isinstance(declared,int)or not 0<=declared<=MAX_BYTES-total:
                raise EvidenceError('Managed worker original byte total exceeds8MiB bound')
            if not isinstance(encoded,str)or len(encoded)!=4*((declared+2)//3):raise EvidenceError('Managed original base64 size differs before expansion')
            try:raw=base64.b64decode(encoded,validate=True)
            except ValueError as error:raise EvidenceError('Invalid managed original base64')from error
            sha=checksum(raw)
            if len(raw)!=declared or sha!=original.get('checksum')or sha!=ref.get('checksum')or declared!=ref.get('size_bytes'):
                raise EvidenceError('Managed worker original bytes/ref checksum mismatch')
            identity=ref['artifact_id']
            if identity in identities:
                if identities[identity]!=sha:raise EvidenceError('Conflicting managed original artifact identity')
                continue
            identities[identity]=sha;total+=len(raw)
            if original.get('content_type')!='application/json' or ref.get('content_type')!='application/json':raise EvidenceError('Managed original evidence must be JSON public boundary objects')
            try:payload=json.loads(raw)
            except (ValueError,UnicodeDecodeError)as error:raise EvidenceError('Managed original JSON is invalid')from error
            if not isinstance(payload,dict):raise EvidenceError('Managed original boundary is not an object')
            _safe_content(payload)
            binding=ref==event.get('binding_ref')
            if binding:
                if sha!=event.get('binding_checksum') or payload.get('experiment_id')!=event['experiment_id']or payload.get('approach_id')!=event['approach_id']or(payload.get('holdings')or{}).get('portfolio_id')!=event['portfolio_id']:
                    raise EvidenceError('Managed original binding belongs to another book/context')
                for field in ('origin_run_id','origin_input_id','scheduled_slot','availability_cutoff'):
                    if payload.get(field)!=event.get(field):raise EvidenceError('Managed original origin context differs')
            elif payload.get('managed_run_id')!=job or payload.get('binding_checksum')!=event.get('binding_checksum'):
                raise EvidenceError('Managed original boundary belongs to another invocation')
            if payload.get('captured_at')and timestamp(payload['captured_at'])>timestamp(event['occurred_at']):raise EvidenceError('Managed original worker input follows terminal event')
            decoded.append((original,ref,raw,payload,binding))
        if artifacts and not any(x[4]for x in decoded):raise EvidenceError('Managed original approved binding missing')
        layers=[];summaries=[];policy=None;policy_issued_at=None
        for index,(original,ref,raw,payload,binding)in enumerate(decoded):
            kind=payload.get('kind')or('approved_binding'if binding else'activity_receipt')
            input_kind=kind in ('role_input','invocation_input')
            if input_kind:
                for pin in ('worker_image_digest','worker_source_commit','worker_code_checksum'):
                    if pin in payload and payload[pin]!=event.get(pin):
                        raise EvidenceError('Managed original worker input pin differs from approved event')
                for pin in ('approved_by','approved_at'):
                    if pin in (payload.get('approval')or{}) and payload['approval'][pin]!=(event.get('approval')or{}).get(pin):
                        raise EvidenceError('Managed original worker input approval differs')
            if kind=='role_input' and payload.get('messages_checksum')!=checksum(canonicalize(payload.get('messages'))):
                raise EvidenceError('Managed original role messages checksum mismatch')
            responses=[p for _,_,_,p,_ in decoded if p.get('input_ref')==ref or p.get('invocation_input_ref')==ref]
            for response in responses:
                if kind=='role_input' and any(response.get(k)!=payload.get(k) for k in ('messages_checksum','call_index','role')):
                    raise EvidenceError('Managed original role response/input association mismatch')
            response_exists=any(p.get('kind')in('role_response','invocation_response','forward_result')or p.get('received_at') for p in responses)
            consumption='binding_not_model_input'if binding else'input_with_original_response_receipt'if input_kind and response_exists else'prepared_input_without_confirmed_response'if input_kind else'output_or_activity_receipt'
            metadata={'input_id':ref['artifact_id'],'run_id':event_id,'kind':'managed_worker_original:'+kind,'captured_at':payload.get('captured_at'),'serialization':'original managed Model artifact bytes','original_ref':ref}
            layers.append({**_portable_capture(metadata,payload,raw,report['artifact_chunks']),'kind':'managed_worker_original:'+kind})
            summaries.append({'index':index,'artifact_id':ref['artifact_id'],'checksum':original['checksum'],'size_bytes':len(raw),'kind':kind,'role':payload.get('role'),'call_index':payload.get('call_index'),'consumption_status':consumption,'captured_at':payload.get('captured_at'),'received_at':payload.get('received_at'),'issued_at':payload.get('issued_at')})
            if kind=='forward_result' and payload.get('policy')is not None:
                policy=payload['policy'];policy_issued_at=payload.get('issued_at')
        normalized={k:v for k,v in event.items()if k!='artifacts'}
        normalized.update(artifact_summaries=summaries,original_artifact_bytes=total,non_trading=True,primary_trading_opportunity=False,counted_in_reporting_window=True,
            evidence_sources=[{'observation_id':row['observation_id'],'checksum':row['checksum']}],policy=policy,decision_time=policy_issued_at,
            model_success=event.get('job_state')=='succeeded'and event.get('evidence_status')=='complete_original_artifacts'and any(s['consumption_status']=='input_with_original_response_receipt'for s in summaries),_original=event)
        events[event_id]=normalized;report['additional_inputs'][event_id]=layers
    for event in events.values():event.pop('_original',None)
    report['managed_inference_events']=sorted(events.values(),key=lambda e:(timestamp(e['occurred_at']),e['event_id']))
    return report
