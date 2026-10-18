"""Unique managed-job costs, charged on actual event time, independent of reuse.

Model/provider estimates never become observed billing. Ancillary day-index events
account for jobs whose approved result expired or was never consumed by a model.
"""
from __future__ import annotations
from datetime import datetime,timezone
import json
import math
from zoneinfo import ZoneInfo

SCHEMA='finplan-managed-model-cost/1'
AMOUNTS=('compute_observed_usd','compute_estimated_usd','vendor_observed_usd','vendor_estimated_usd')


def instant(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('Managed cost event requires timezone-qualified actual time')
    return parsed.astimezone(timezone.utc)


def collect_managed_cost_events(runs,ancillary,*,experiment_id,registry,start_day,end_day,generated_at):
    known={a['approach_id']:a for a in registry};unique={};sources={}
    def accept(event,origin,reference):
        if not isinstance(event,dict) or event.get('protocol')!=SCHEMA:
            raise ValueError('Managed cost event protocol invalid')
        eid=event.get('event_id');job=event.get('managed_run_id')
        if not job or eid!='managed:'+job:raise ValueError('Managed cost event ID does not match its permanent job')
        aid=event.get('approach_id');approach=known.get(aid)
        if not approach or (event.get('experiment_id'),event.get('portfolio_id'))!=(experiment_id,approach['portfolio_id']):
            raise ValueError('Managed cost event belongs to another experiment/approach/book')
        if (origin.get('experiment_id'),origin.get('approach_id'),origin.get('portfolio_id'))!=(experiment_id,aid,event['portfolio_id']):
            raise ValueError('Managed cost event origin binding mismatch')
        at=instant(event['occurred_at'])
        if at>instant(generated_at):raise ValueError('Managed cost event follows report availability cutoff')
        for field in AMOUNTS:
            value=event.get(field)
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0):
                raise ValueError('Managed cost event amount must be nonnegative finite USD or unavailable')
        for category in ('compute','vendor'):
            basis=str(event.get(category+'_cost_basis','unavailable')).lower()
            observed=event.get(category+'_observed_usd')
            usage=event.get('usage') or {}
            verified_no_call=category=='vendor' and isinstance(usage,dict) and usage.get('transport_calls',usage.get('calls'))==0
            verified_no_invocation=event.get('invocation_state')=='not_invoked'
            verified_zero=observed==0 and (verified_no_call or verified_no_invocation or basis.startswith('observed no '))
            if observed is not None and ('estimated' in basis or 'estimate' in basis) and not verified_zero:
                raise ValueError('Estimated managed job cost falsely labeled observed billing')
        canonical=json.dumps(event,sort_keys=True,separators=(',',':'),allow_nan=False)
        if eid in unique and unique[eid][1]!=canonical:
            raise ValueError('Conflicting immutable managed cost event for the same permanent job')
        unique[eid]=(event,canonical);sources.setdefault(eid,[]).append(reference)
    for observation in ancillary:
        kind=str(observation.get('kind',''))
        if kind=='managed_model_cost' or kind.startswith('managed_model_cost:'):
            event=observation.get('payload') or observation
            if observation.get('evidence_error') or not event:
                raise ValueError('Managed cost event has unavailable/corrupt immutable evidence')
            if instant(observation['slot'])!=instant(event['occurred_at']):
                raise ValueError('Managed cost event day index does not match actual occurrence')
            accept(event,observation,{'observation_id':observation['observation_id'],'checksum':observation.get('checksum'),'artifact_key':observation.get('artifact_key')})
    for run in runs:
        event=run.get('managed_cost_event') or (run.get('output') or run.get('outcome') or {}).get('managed_cost_event')
        if event:
            accept(event,run,{'run_id':run['run_id'],'source':'embedded_reuse_reference_not_a_new_charge'})
    result=[]
    for eid,(event,_) in sorted(unique.items()):
        day=instant(event['occurred_at']).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        result.append({**event,'reporting_day':day,'counted_in_reporting_window':start_day<=day<=end_day,
                       'evidence_sources':sources[eid],'accounting':'once_per_permanent_job_at_actual_occurred_at'})
    return result


def managed_cost_summary(events,*,start=None,end=None):
    selected=[e for e in events if e.get('counted_in_reporting_window') and (start is None or instant(e['occurred_at'])>=instant(start)) and (end is None or instant(e['occurred_at'])<=instant(end))]
    result={'event_count':len(selected),'event_ids':[e['event_id']for e in selected],
            'unknown_observed_event_count':sum(any(e.get(k) is None for k in ('compute_observed_usd','vendor_observed_usd'))for e in selected),
            'accounting':'unique_actual_job_events_not_recommendation_reuse_count'}
    for category in ('compute','vendor'):
        observed=category+'_observed_usd';estimated=category+'_estimated_usd'
        result[observed]=sum(float(e[observed])for e in selected)if all(e.get(observed)is not None for e in selected)else None
        result['known_'+observed]=sum(float(e[observed])for e in selected if e.get(observed)is not None)
        values=[float(e[estimated])for e in selected if e.get(estimated)is not None]
        result[estimated]=sum(values)if values else None
        result[category+'_cost_basis']=sorted({str(e.get(category+'_cost_basis','unavailable'))for e in selected})
    result['total_observed_usd']=sum(result[k]for k in ('compute_observed_usd','vendor_observed_usd'))if all(result[k]is not None for k in ('compute_observed_usd','vendor_observed_usd'))else None
    return result


def apply_managed_costs(books,events,registry):
    known={a['approach_id']:a for a in registry}
    for book in books:
        selected=[e for e in events if e['approach_id']==book['approach_id']]
        if not selected:continue
        total=managed_cost_summary(selected)
        comparison=managed_cost_summary(selected,start=known[book['approach_id']].get('comparison_horizon_start'))
        observed=managed_cost_summary(selected,start=book.get('window_start'),end=book.get('window_end')) if book.get('window_start')and book.get('window_end')else None
        book.update(managed_costs=total,comparison_managed_costs=comparison,observed_window_managed_costs=observed,
                    invocation_model_cost=book.get('model_cost'),invocation_comparison_model_cost=book.get('comparison_model_cost'),invocation_observed_window_model_cost=book.get('observed_model_cost'))
        for field,cost in [('model_cost',total),('comparison_model_cost',comparison),('observed_model_cost',observed)]:
            if cost is None:continue
            base=book.get(field)
            if base is None and book.get('recommendations')==0:base=0.0
            book[field]=base+cost['total_observed_usd']if base is not None and cost['total_observed_usd']is not None else None
        book.setdefault('model_cost_basis',[]).append('managed_job_event_observed'if total['total_observed_usd']is not None else'managed_job_event_observed_cost_unavailable; estimates separate')
