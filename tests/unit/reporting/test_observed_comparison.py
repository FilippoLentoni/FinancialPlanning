"""Controlled marks prove observed ranking without inventing missing values."""
import copy
from zoneinfo import ZoneInfo
from datetime import datetime,timedelta,timezone
from finplan_platform.reporting.reports import aggregate,canonical,checksum,summarize
from test_modeling_reports import Reader,registry,report,T0,T1,T2


def test_partial_first_calendar_day_and_common_missed_first_hour_rank_observed_week():
    reader=Reader();reader.runs=[];reader.observations=[];reader.inputs={}
    initial=datetime(2026,10,18,tzinfo=timezone.utc)
    for aid in ['cash','rule']:
        for n in range(148):
            at=(initial+timedelta(hours=n)).isoformat();rid=f'{aid}_{n}';iid='i_'+rid
            payload={'input_id':iid,'run_id':rid,'approach_id':aid,'decision_time':at,'availability_cutoff':at,'holdings':{'cash':100,'revision':1},'numeric':{'prepared':{'controlled':n}},'news':{'consumed':False,'items':[]}}
            raw=canonical(payload);reader.inputs[iid]={'metadata':{'input_id':iid,'run_id':rid,'approach_id':aid,'kind':'model_input','checksum':checksum(raw),'captured_at':at},'payload':payload,'raw_bytes':raw}
            reader.runs.append({'experiment_id':'test','approach_id':aid,'portfolio_id':'book_'+aid,'run_id':rid,'input_id':iid,'scheduled_slot':at,'decision_time':at,'availability_cutoff':at,'status':'skipped' if n==0 else 'hold','reason':'common missed first hour' if n==0 else 'controlled hold','execution_cost':0,'model_cost':0})
            reader.observations.append({'experiment_id':'test','approach_id':aid,'portfolio_id':'book_'+aid,'run_id':rid,'observation_id':'o_'+rid,'observed_at':at,'value':None if n==0 else 100+(n/100 if aid=='rule' else 0),'quote_status':'missing' if n==0 else 'fresh','cash':100,'market_session':{'status':'open','session_date':(initial+timedelta(hours=n)).astimezone(ZoneInfo('America/New_York')).date().isoformat(),'source':'CONTROLLED_CRYPTO_CALENDAR'}})
    def query_day(experiment,day,*,kind,page_size=100,next_token=None):
        rows=reader.runs if kind=='runs' else reader.observations
        return [copy.deepcopy(o) for o in rows if datetime.fromisoformat(o.get('decision_time',o.get('observed_at'))).astimezone(ZoneInfo('America/New_York')).date().isoformat()==day],None
    reader.query_day=query_day
    reg=registry()
    for row in reg:row['market_calendar']='CRYPTO_24X7'
    result=aggregate(reader,experiment_id='test',registry=reg,start_day='2026-10-17',end_day='2026-10-23',generated_at='2026-10-24T04:00:00Z',finalized=True,mode='controlled_replay',comparison_policy={'first_scheduled_slot':initial.isoformat(),'cadence_seconds':3600})
    rule=next(b for b in result['leaderboard'] if b['approach_id']=='rule')
    assert rule['rank']==1 and rule['ranking_scope']=='observed_shared_window'
    assert rule['window_start']=='2026-10-18T01:00:00+00:00' and rule['window_end']=='2026-10-24T03:00:00+00:00'
    assert rule['observed_comparison_valuations']==147 and rule['failure_rate']==1/148
    assert rule['coverage']==147/148 and not rule['full_calendar_coverage_complete']
    assert any('Partial first calendar day' in w for w in rule['coverage_warnings'])
    assert len(result['runs'])==296 and len(result['observations'])==296


def test_unique_missing_mark_uses_intersection_preserves_full_evidence_and_gap_fees():
    r=Reader();r.observations[4]['value']=None
    events=[{'approach_id':'rule','portfolio_id':'book_rule','observation_id':'fee','observed_at':T1,'transaction_cost':.5,'simulated_fills':[{'notional':50}],'execution':{}}]
    books=summarize(registry(),r.runs,r.observations,[],events);rule=next(b for b in books if b['approach_id']=='rule')
    assert rule['rank']==1 and rule['observation_window']==[T0,T2]
    assert rule['execution_cost']==.5 and rule['turnover']==.5 and rule['observed_execution_event_ids']==['fee']
    assert rule['full_period_metrics']['current_value']==102 and rule['coverage_warnings']
    assert r.observations[4]['value'] is None


def test_cropped_cashflow_window_requires_matching_persisted_return_not_global_twr():
    r=Reader();r.observations[3]['value']=None;r.observations[-1].update(value=111,external_cash_flow=10,time_weighted_return=.1)
    books=summarize(registry(),r.runs,r.observations,[],[]);rule=next(b for b in books if b['approach_id']=='rule')
    # Full original valid rule window already startsT1: its persisted TWR matches.
    assert rule['observed_external_cash_flows']==10 and rule['net_pnl']==0
    # Cash has an external flow during the cropped shared interval, but its TWR
    # belonged toT0..T2, so it cannot be substituted forT1..T2.
    r.observations[2].update(value=110,external_cash_flow=10,time_weighted_return=.2)
    cash=next(b for b in summarize(registry(),r.runs,r.observations,[],[]) if b['approach_id']=='cash')
    assert cash['net_return'] is None and cash['rank'] is None


def test_no_shared_pair_different_universe_and_unavailable_approach_remain_unranked():
    r=Reader();r.runs[2]['status']=r.runs[3]['status']='unavailable'
    assert all(b['rank'] is None for b in summarize(registry(),r.runs,r.observations,[],[]))
    r=Reader();reg=registry();reg[1]['investable_universe']=['OTHER']
    assert all(b['rank'] is None for b in summarize(reg,r.runs,r.observations,[],[]))
    r.observations=[o for o in r.observations if (o['approach_id']=='cash' and o['observed_at']!=T2) or (o['approach_id']=='rule' and o['observed_at']!=T0)]
    assert all(b['rank'] is None for b in summarize(registry(),r.runs,r.observations,[],[]))


def test_tied_observed_returns_use_shared_drawdown_then_approach_id():
    r=Reader()
    for o in r.observations:
        o['value']=100 if o['observed_at']!=T1 else (90 if o['approach_id']=='rule' else 99)
    books=summarize(registry(),r.runs,r.observations,[],[])
    assert books[0]['approach_id']=='cash' and books[0]['rank']==1 and all(b['return_tied'] for b in books)
    assert books[0]['max_drawdown']==.01 and books[1]['max_drawdown']==.1


def test_pending_corporate_action_cannot_be_hidden_by_ranking_older_valid_window():
    r=Reader();r.observations[-1].update(value=None,quote_status='corporate_action_unreconciled',corporate_action_status='pending_latched_action_evidence')
    books=summarize(registry(),r.runs,r.observations,[],[])
    rule=next(b for b in books if b['approach_id']=='rule')
    assert rule['rank'] is None and any('unresolved corporate action' in reason for reason in rule['ranking_blockers'])
