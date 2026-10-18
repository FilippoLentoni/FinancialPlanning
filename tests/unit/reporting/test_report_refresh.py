"""Durable late-cost revisions use strong receipts, immutable archives and safe acks."""
from datetime import datetime,timezone
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from finplan_platform.core.modeling_evidence import ModelingEvidence
from finplan_platform.reporting import refresh
from finplan_platform.reporting.reports import aggregate,write_report,EvidenceError
from scripts import modeling_report as cli
from tests.unit.reporting.test_managed_costs import event


def fixture(backend):
    at='2026-01-12T20:00:00Z';observed='2026-01-13T15:00:00Z'
    book=backend.ensure_portfolio('experiment','jev',1000,['SPY'],at)
    e=event(portfolio_id=book['portfolio_id'])
    row=backend.record_observation('experiment','jev',at,book['portfolio_id'],e,observed,kind='managed_model_cost:job1')
    task={'protocol':refresh.DAILY,'experiment_id':'experiment','reporting_day':'2026-01-12','event_id':e['event_id'],'observation_id':row['observation_id'],'checksum':row['checksum']}
    registry=[{'approach_id':'jev','portfolio_id':book['portfolio_id'],'family':'language_model','comparison_policy_id':'controlled','cohort_id':'controlled'}]
    return task,registry


def test_strong_known_receipt_overcomes_gsi_lag_without_duplicate_cost_or_ledger(repo,artifacts,tmp_path,monkeypatch):
    backend=ModelingEvidence(repo,artifacts);task,registry=fixture(backend)
    refresh.put_task(tmp_path/'queue',task)
    tasks,state=refresh.read_tasks(tmp_path/'queue',['experiment'])
    receipts=refresh.verify_receipts(backend,tasks)
    original=repo.query_index
    def lag(table,*a,**kw):return ([],None)if table=='portfolio_observation'else original(table,*a,**kw)
    monkeypatch.setattr(repo,'query_index',lag)
    report=aggregate(backend,experiment_id='experiment',registry=registry,start_day='2026-01-12',generated_at='2026-01-13T16:00:00Z',required_observations=receipts,mode='controlled_replay')
    refresh.ensure_in_report(report,tasks)
    assert report['leaderboard'][0]['managed_costs']['total_observed_usd']==pytest.approx(1.5)
    assert report['observations']==[] and report['execution_events']==[]
    assert len(report['ancillary_evidence'])==1
    written=write_report(report,tmp_path/'reports',update_latest=False)
    assert task['observation_id']in written['manifest']['source_record_ids']['observations']
    refresh.acknowledge(tasks)
    assert not list((tmp_path/'queue').glob('*.json'))
    monkeypatch.setattr(repo,'query_index',original)
    second=aggregate(backend,experiment_id='experiment',registry=registry,start_day='2026-01-12',generated_at='2026-01-13T16:00:00Z',required_observations=receipts,mode='controlled_replay')
    assert second['leaderboard'][0]['managed_costs']['event_count']==1
    assert len(second['ancillary_evidence'])==1
    assert backend.get_portfolio(registry[0]['portfolio_id'])['revision']==1


def test_receipt_mismatch_missing_archive_and_concurrent_new_tasks_are_not_acknowledged(repo,artifacts,tmp_path):
    backend=ModelingEvidence(repo,artifacts);task,_=fixture(backend)
    path=refresh.put_task(tmp_path,task);tasks,_=refresh.read_tasks(tmp_path,['experiment'])
    modified={**task,'checksum':'sha256:'+'0'*64};bad=[{**tasks[0],'task':modified}]
    with pytest.raises(EvidenceError,match='checksum mismatch'):refresh.verify_receipts(backend,bad)
    with pytest.raises(EvidenceError,match='not present'):refresh.ensure_in_report({'managed_cost_events':[]},tasks)
    assert path.exists()
    later={**task,'event_id':'managed:job2','observation_id':'later'}
    later_path=refresh.put_task(tmp_path,later)
    refresh.acknowledge(tasks)
    assert later_path.exists()and not path.exists()
    tasks,_=refresh.read_tasks(tmp_path,['experiment']);later_path.write_text('{"concurrent":"replacement"}')
    refresh.acknowledge(tasks)
    assert later_path.exists()


def test_weekly_tasks_only_existing_seven_windows_and_versioned_idempotence(tmp_path):
    queue=tmp_path/'queue';output=tmp_path/'reports';weekly=output/'weekly';weekly.mkdir(parents=True)
    for name in ['2026-01-06_2026-01-12','2026-01-08_2026-01-14']:(weekly/(name+'.json')).write_text('{}')
    task={'protocol':refresh.DAILY,'experiment_id':'experiment','reporting_day':'2026-01-12','event_id':'managed:job','observation_id':'observation','checksum':'checksum'}
    records=[{'task':task}]
    created=refresh.enqueue_existing_weekly(records,queue_dir=queue,output_dir=output,daily_revision='revision1')
    assert len(created)==2
    assert refresh.enqueue_existing_weekly(records,queue_dir=queue,output_dir=output,daily_revision='revision1')==created
    refresh.enqueue_existing_weekly(records,queue_dir=queue,output_dir=output,daily_revision='revision2')
    tasks,state=refresh.read_tasks(queue/'weekly',['experiment'],weekly=True)
    assert len(tasks)==4 and state['pending_files']==4
    assert {r['task']['source_daily_revision']for r in tasks}=={'revision1','revision2'}


def test_bounded_queue_keeps_overflow_and_different_experiments(tmp_path,monkeypatch):
    for n in range(5):refresh.put_task(tmp_path,{'protocol':refresh.DAILY,'experiment_id':'experiment','reporting_day':'2026-01-12','event_id':'managed:'+str(n),'observation_id':str(n),'checksum':str(n)})
    monkeypatch.setattr(refresh,'MAX_TASKS',2)
    selected,state=refresh.read_tasks(tmp_path,['experiment'])
    assert len(selected)==2 and state['overflow_retained']and len(list(tmp_path.glob('*.json')))==5
    other,_=refresh.read_tasks(tmp_path,['other']);assert other==[]
    monkeypatch.setattr(refresh,'MAX_PENDING_FILES',5)
    with pytest.raises(EvidenceError,match='queue full'):
        refresh.put_task(tmp_path,{'protocol':refresh.DAILY,'experiment_id':'experiment','reporting_day':'2026-01-12','event_id':'managed:new','observation_id':'new','checksum':'new'})
    assert len(list(tmp_path.glob('*.json')))==5


def test_empty_weekly_repair_does_not_access_aws_or_ordinary_report(monkeypatch,tmp_path):
    monkeypatch.setenv('FINPLAN_REPORT_REFRESH_DIR',str(tmp_path))
    monkeypatch.setattr(ModelingEvidence,'from_ec2',lambda **kw:pytest.fail('Empty repair contacted AWS'))
    assert cli.main(['--experiment','experiment','--repair-weekly-from-queue'])==0


def test_cli_daily_repair_before_current_then_one_weekly_revision_preserves_newer_latest(repo,artifacts,tmp_path,monkeypatch):
    from finplan_platform.reporting.transport import decode_transport
    backend=ModelingEvidence(repo,artifacts);task,registry=fixture(backend)
    backend.register_approaches('experiment',registry,{},'2026-01-12T14:00:00Z')
    queue=tmp_path/'queue';output=tmp_path/'reports'
    # Both prior windows exist, and latest points at the newer one.
    for start,end in [('2026-01-06','2026-01-12'),('2026-01-07','2026-01-13')]:
        report=aggregate(backend,experiment_id='experiment',registry=registry,start_day=start,end_day=end,generated_at='2026-01-13T16:00:00Z',mode='controlled_replay')
        write_report(report,output)
    latest_weekly=(output/'replay'/'weekly'/'latest.json').read_bytes()
    refresh.put_task(queue,task)
    monkeypatch.setenv('FINPLAN_REPORT_REFRESH_DIR',str(queue))
    monkeypatch.setattr(ModelingEvidence,'from_ec2',lambda **kw:backend)
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,1,13,16,0,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(cli,'datetime',Frozen)
    args=['--experiment','experiment','--day','2026-01-13','--mode','controlled_replay','--output',str(output),'--no-include-interim','--no-s3-archive']
    assert cli.main(args)==0
    assert not list(queue.glob('*.json'))
    daily=decode_transport(json.loads((output/'replay'/'2026-01-12.json').read_bytes()))
    current=decode_transport(json.loads((output/'replay'/'latest.json').read_bytes()))
    assert daily['managed_cost_events'][0]['event_id']==task['event_id']
    assert current['end_day']=='2026-01-13'
    pending=list((queue/'weekly').glob('*.json'));assert len(pending)==2
    assert cli.main(args+['--repair-weekly-from-queue'])==0
    assert len(list((queue/'weekly').glob('*.json')))==1
    assert (output/'replay'/'weekly'/'latest.json').read_bytes()==latest_weekly
    repaired=decode_transport(json.loads((output/'replay'/'weekly'/'2026-01-06_2026-01-12.json').read_bytes()))
    assert repaired['managed_cost_events'][0]['evidence_sources'][0]['observation_id']==task['observation_id']


def test_failed_archive_keeps_daily_receipt_and_latest_alias_intact(repo,artifacts,tmp_path,monkeypatch):
    backend=ModelingEvidence(repo,artifacts);task,registry=fixture(backend)
    backend.register_approaches('experiment',registry,{},'2026-01-12T14:00:00Z')
    queue=tmp_path/'queue';path=refresh.put_task(queue,task)
    monkeypatch.setenv('FINPLAN_REPORT_REFRESH_DIR',str(queue))
    monkeypatch.setattr(ModelingEvidence,'from_ec2',lambda **kw:backend)
    monkeypatch.setattr(cli,'write_report',lambda *a,**kw:(_ for _ in ()).throw(OSError('controlled archive failure')))
    with pytest.raises(OSError,match='archive failure'):
        cli.main(['--experiment','experiment','--day','2026-01-13','--mode','controlled_replay','--output',str(tmp_path/'reports'),'--no-include-interim','--no-s3-archive'])
    assert path.exists()and not list((tmp_path/'reports').glob('latest.*'))


def test_oversized_legacy_queue_can_drain_with_bounded_lower_bound_visibility(tmp_path,monkeypatch):
    for n in range(6):refresh.put_task(tmp_path,{'protocol':refresh.DAILY,'experiment_id':'experiment','reporting_day':'2026-01-12','event_id':'managed:'+str(n),'observation_id':str(n),'checksum':str(n)})
    monkeypatch.setattr(refresh,'MAX_PENDING_FILES',3)
    tasks,state=refresh.read_tasks(tmp_path,['experiment'])
    assert len(tasks)==3 and state['pending_count_is_lower_bound']and state['overflow_retained']is True
    refresh.acknowledge(tasks)
    assert len(list(tmp_path.glob('*.json')))==3


def test_monotonic_skip_retains_cost_task_until_exact_dated_alias_published(repo,artifacts,tmp_path,monkeypatch):
    from finplan_platform.reporting.transport import decode_transport
    backend=ModelingEvidence(repo,artifacts);task,registry=fixture(backend)
    backend.register_approaches('experiment',registry,{},'2026-01-12T14:00:00Z')
    queue=tmp_path/'queue';output=tmp_path/'reports';path=refresh.put_task(queue,task)
    # A concurrently newer snapshot can lack the cost because its day-index read
    # lagged. The older writer's strong task receipt is complete, but its alias
    # publication must not regress the newer snapshot or acknowledge unseen cost.
    original=repo.query_index
    monkeypatch.setattr(repo,'query_index',lambda table,*a,**kw:([],None) if table=='portfolio_observation' else original(table,*a,**kw))
    incomplete=aggregate(backend,experiment_id='experiment',registry=registry,start_day='2026-01-12',generated_at='2026-01-13T17:00:00Z',mode='controlled_replay')
    prior=write_report(incomplete,output,update_latest=False)
    before=(output/'replay'/'2026-01-12.json').read_bytes()
    monkeypatch.setenv('FINPLAN_REPORT_REFRESH_DIR',str(queue));monkeypatch.setattr(ModelingEvidence,'from_ec2',lambda **kw:backend)
    class Frozen(datetime):
        hour=16
        @classmethod
        def now(cls,tz=None):return cls(2026,1,13,cls.hour,0,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(cli,'datetime',Frozen)
    args=['--experiment','experiment','--day','2026-01-13','--mode','controlled_replay','--output',str(output),'--no-include-interim','--no-s3-archive']
    with pytest.raises(EvidenceError,match='dated alias was not published'):cli.main(args)
    assert path.exists() and (output/'replay'/'2026-01-12.json').read_bytes()==before
    assert not list((queue/'weekly').glob('*.json'))
    assert len(list((output/'replay'/'archive'/'2026-01-12').iterdir()))==2 # Complete repair archive retained.
    Frozen.hour=18
    assert cli.main(args)==0 and not path.exists()
    published=decode_transport(json.loads((output/'replay'/'2026-01-12.json').read_bytes()))
    assert published['managed_cost_events'][0]['event_id']==task['event_id']


def test_cli_retry_protocol_only_defers_publication_race(monkeypatch,capsys):
    from finplan_platform.reporting.reports import PublicationDeferred
    monkeypatch.setattr(cli,'main',lambda argv:(_ for _ in ()).throw(PublicationDeferred('retained task')))
    assert cli.cli_exit_code([])==75
    assert 'publication deferred' in capsys.readouterr().err
    monkeypatch.setattr(cli,'main',lambda argv:(_ for _ in ()).throw(EvidenceError('unsafe artifact')))
    assert cli.cli_exit_code([])==2
    assert 'evidence failure' in capsys.readouterr().err
