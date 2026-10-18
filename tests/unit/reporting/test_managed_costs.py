"""Managed-job costs occur once, on their actual day, independently of reuse."""
from copy import deepcopy
import pytest
from finplan_platform.reporting.costs import collect_managed_cost_events,managed_cost_summary,apply_managed_costs

REGISTRY=[{'approach_id':'jev','portfolio_id':'book','comparison_horizon_start':'2026-01-12T14:00:00Z'}]

def event(**extra):
    return {'protocol':'finplan-managed-model-cost/1','event_id':'managed:job1','managed_run_id':'job1',
            'experiment_id':'experiment','approach_id':'jev','portfolio_id':'book','occurred_at':'2026-01-12T20:00:00Z',
            'compute_observed_usd':1.2,'vendor_observed_usd':.3,'compute_estimated_usd':1.1,'vendor_estimated_usd':.2,
            'compute_cost_basis':'observed_billing','vendor_cost_basis':'observed_vendor_usage','source_evidence_refs':[{'checksum':'source'}],**extra}

def run(e,number):
    return {'experiment_id':'experiment','approach_id':'jev','portfolio_id':'book','run_id':'run'+str(number),'model_cost':0,'managed_cost_event':deepcopy(e)}

def observation(e):
    return {'kind':'managed_model_cost:'+e['managed_run_id'],'experiment_id':'experiment','approach_id':'jev','portfolio_id':'book','observation_id':'observation1','slot':e['occurred_at'],
            'observed_at':'2026-01-13T15:00:00Z','checksum':'event-original-checksum','payload':deepcopy(e)}

def collect(runs=(),observations=(),start='2026-01-12',end='2026-01-12'):
    return collect_managed_cost_events(runs,observations,experiment_id='experiment',registry=REGISTRY,start_day=start,end_day=end,generated_at='2026-01-13T16:00:00Z')


def test_duplicate_reused_rows_and_independent_event_charge_once_on_actual_day():
    e=event();events=collect([run(e,n)for n in range(24)],[observation(e)])
    summary=managed_cost_summary(events)
    assert len(events)==1 and len(events[0]['evidence_sources'])==25
    assert summary['event_count']==1 and summary['total_observed_usd']==pytest.approx(1.5)
    assert summary['compute_estimated_usd']==1.1 and summary['vendor_estimated_usd']==.2
    tomorrow=collect([run(e,n)for n in range(24)],start='2026-01-13',end='2026-01-13')
    assert tomorrow[0]['counted_in_reporting_window'] is False
    assert managed_cost_summary(tomorrow)['event_count']==0
    assert managed_cost_summary(tomorrow)['total_observed_usd']==0


def test_unconsumed_expired_job_is_accounted_from_actual_day_event_without_any_recommendation():
    events=collect(observations=[observation(event(job_state='expired_unconsumed',invocation_state='completed'))])
    assert managed_cost_summary(events)['total_observed_usd']==pytest.approx(1.5)
    books=[{'approach_id':'jev','model_cost':None,'comparison_model_cost':None,'recommendations':0}]
    apply_managed_costs(books,events,REGISTRY)
    assert books[0]['model_cost']==pytest.approx(1.5)
    assert books[0]['managed_costs']['event_ids']==['managed:job1']


def test_unknown_observed_cost_stays_unknown_and_estimates_are_separate():
    e=event(compute_observed_usd=None,compute_estimated_usd=4.5,compute_cost_basis='estimated_instance_seconds_rate')
    summary=managed_cost_summary(collect(observations=[observation(e)]))
    assert summary['compute_observed_usd'] is None and summary['total_observed_usd'] is None
    assert summary['known_vendor_observed_usd']==.3
    assert summary['compute_estimated_usd']==4.5 and summary['unknown_observed_event_count']==1
    with pytest.raises(ValueError,match='falsely labeled'):
        collect(observations=[observation(event(compute_cost_basis='estimated_rate'))])


def test_observed_window_and_comparison_horizon_do_not_charge_outside_job_times():
    e=event();events=collect([run(e,1)],[observation(e)])
    books=[{'approach_id':'jev','model_cost':.1,'comparison_model_cost':.1,'observed_model_cost':.1,'recommendations':1,
            'window_start':'2026-01-12T21:00:00Z','window_end':'2026-01-12T22:00:00Z'}]
    apply_managed_costs(books,events,REGISTRY)
    assert books[0]['model_cost']==pytest.approx(1.6)
    assert books[0]['observed_model_cost']==.1
    assert books[0]['observed_window_managed_costs']['event_count']==0
    assert managed_cost_summary(events,start='2026-01-12T20:00:00Z',end='2026-01-12T20:00:00Z')['event_count']==1


def test_conflicting_duplicate_wrong_book_and_backdated_index_are_rejected():
    e=event()
    with pytest.raises(ValueError,match='Conflicting immutable'):
        collect([run(event(vendor_observed_usd=.4),1)],[observation(e)])
    with pytest.raises(ValueError,match='another experiment'):
        collect([run(event(portfolio_id='other'),1)])
    wrong=observation(e);wrong['slot']='2026-01-13T20:00:00Z'
    with pytest.raises(ValueError,match='day index'):
        collect(observations=[wrong])
    with pytest.raises(ValueError,match='nonnegative'):
        collect(observations=[observation(event(vendor_observed_usd=-1))])


def test_multi_day_same_second_jobs_distinct_ids_and_new_york_dst_date():
    one=event(occurred_at='2026-11-01T05:30:00Z');two=event(event_id='managed:job2',managed_run_id='job2',occurred_at=one['occurred_at'])
    observations=[observation(one),observation(two)]
    # The observer is after both actual events; no UTC-to-local day substitution.
    events=collect_managed_cost_events([],observations,experiment_id='experiment',registry=REGISTRY,start_day='2026-11-01',end_day='2026-11-01',generated_at='2026-11-02T16:00:00Z')
    assert len(events)==2 and all(e['reporting_day']=='2026-11-01'for e in events)
    assert managed_cost_summary(events)['total_observed_usd']==pytest.approx(3)


def test_real_protocol_never_started_and_no_vendor_call_zero_keep_separate_estimates():
    e=event(compute_observed_usd=0,compute_estimated_usd=.15,compute_cost_basis='observed no managed compute started; unspent native upper-bound estimate separate',
            vendor_observed_usd=0,vendor_estimated_usd=0,vendor_cost_basis='observed no vendor invocation',invocation_state='not_invoked',job_state='CANCELLED')
    cost=managed_cost_summary(collect(observations=[observation(e)]))
    assert cost['total_observed_usd']==0 and cost['compute_estimated_usd']==.15
    no_call=event(compute_observed_usd=None,compute_cost_basis='billing unavailable; native preflight upper-bound estimate separate, never elapsed-time observed billing',
                  vendor_observed_usd=0,vendor_cost_basis='estimated public vendor prices separate',usage={'transport_calls':0},invocation_state='completed_no_call')
    cost=managed_cost_summary(collect(observations=[observation(no_call)]))
    assert cost['compute_observed_usd']is None and cost['vendor_observed_usd']==0
    with pytest.raises(ValueError,match='falsely labeled'):
        collect(observations=[observation(event(compute_observed_usd=.15,compute_cost_basis='estimated instance rate',invocation_state='invoked'))])


@pytest.mark.parametrize('name',['never_started_cancelled','completed_no_call','positive_estimated','unknown_indeterminate'])
def test_actual_model_terminal_cost_event_wire_fixtures(name):
    import json
    from pathlib import Path
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # Generated by FinanceModel terminal_cost_event itself (controlled public
    # descriptors, no AWS/model/provider calls), rather than a renderer-shaped stub.
    fixtures=json.loads((Path(__file__).resolve().parents[2]/'fixtures'/'reporting'/'managed-cost-producer-events.json').read_bytes())
    e=fixtures[name];unchanged=json.dumps(e,sort_keys=True)
    day=datetime.fromisoformat(e['occurred_at'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
    row={'kind':'managed_model_cost:'+e['managed_run_id'],'experiment_id':e['experiment_id'],'approach_id':e['approach_id'],'portfolio_id':e['portfolio_id'],
         'observation_id':'controlled-'+name,'slot':e['occurred_at'],'payload':e,'checksum':'controlled-original-checksum'}
    reg=[{'approach_id':e['approach_id'],'portfolio_id':e['portfolio_id']}]
    actual=collect_managed_cost_events([], [row],experiment_id=e['experiment_id'],registry=reg,start_day=day,end_day=day,generated_at='2026-07-14T00:00:00Z')
    summary=managed_cost_summary(actual)
    assert summary['event_count']==1 and summary['compute_estimated_usd']==e['compute_estimated_usd']
    assert json.dumps(e,sort_keys=True)==unchanged
    if name=='never_started_cancelled':assert summary['total_observed_usd']==0
    else:assert summary['compute_observed_usd']is None and summary['total_observed_usd']is None
    if name=='completed_no_call':assert summary['vendor_observed_usd']==0


def test_news_attribution_annotations_preserve_original_bytes_and_never_label_gdelt_as_rss(tmp_path):
    import subprocess
    from pathlib import Path
    script=(Path(__file__).resolve().parents[3]/'platform'/'finplan_platform'/'reporting'/'report.js').read_text()
    # Execute the actual function body with its actual escaping/preview dependency
    # replaced by a safe deterministic renderer; no browser/network/model calls.
    start=script.index('function newsView(');end=script.index('function subBoundaryInputs(',start)
    body=script[start:end]
    runner="const esc=v=>String(v??'').replaceAll('<','&lt;');const preview=v=>JSON.stringify(v);"+body+"\n"+'''
const legacy={consumed:true,items:[{provider:'Google News RSS',headline:'A market update',symbols:['A'],searched_symbols:['A']}]};
const before=JSON.stringify(legacy);const html=newsView(legacy);if(!html.includes('unverified constituent attribution'))throw new Error('Missing legacy warning');if(JSON.stringify(legacy)!==before)throw new Error('Changed original payload');
const gdelt={consumed:true,metadata_only:true,items:[{provider:'GDELT',symbols:['A'],tone:-1}]};if(newsView(gdelt).includes('unverified constituent attribution'))throw new Error('RSS label falsely applied to GDELT');
const modern={consumed:true,items:[{provider:'Google News RSS',headline:'Agilent update',symbols:['A'],symbol_attribution:{version:'finplan-constituent-attribution/1',method:'dated_security_name_or_explicit_ticker',universe_checksum:'controlled',universe_as_of:'2026-01-12',matches:[{symbol:'A',method:'security_name',matched_content:'Agilent',security_name:'Agilent',worker_id:'controlled'}],excluded_matches:[{symbol:'A',matched_content:'A',reason:'ambiguous_bare_token',worker_id:'controlled'}]}}]};if(!newsView(modern).includes('Recorded symbol attribution method: dated_security_name_or_explicit_ticker'))throw new Error('Missing actual method');
'''
    result=subprocess.run(['node','-e',runner],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
