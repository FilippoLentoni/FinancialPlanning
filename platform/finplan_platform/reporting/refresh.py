"""Private bounded durable receipts requesting report revisions, never model runs."""
from __future__ import annotations
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import date,timedelta
from zoneinfo import ZoneInfo

from .reports import EvidenceError,checksum,timestamp

DAILY='finplan-report-refresh/1'
WEEKLY='finplan-weekly-report-refresh/1'
MAX_TASKS=128
MAX_PENDING_FILES=10000
MAX_TASK_BYTES=16384
NAME=re.compile(r'[0-9a-f]{64}\.json')


def directory(path):
    path=Path(path)
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    if path.is_symlink()or path.stat().st_uid!=os.getuid():raise EvidenceError('Report refresh directory must be private and owned')
    path.chmod(0o700)
    return path


def read_tasks(path,experiments,*,weekly=False):
    path=directory(path);selected=[];total=0
    # scandir does not materialize an unbounded list. Overflow is visible and
    # retained; no task is deleted merely because an invocation reaches a bound.
    with os.scandir(path) as entries:
        for entry in entries:
            if not NAME.fullmatch(entry.name):continue
            total+=1
            if total>MAX_PENDING_FILES:break
            if entry.is_symlink():raise EvidenceError('Refresh task cannot be a symbolic link')
            stat=entry.stat(follow_symlinks=False)
            if stat.st_uid!=os.getuid()or stat.st_size>MAX_TASK_BYTES:raise EvidenceError('Invalid private refresh task ownership/size')
            fd=os.open(entry.path,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,"rb")as stream:raw=stream.read(MAX_TASK_BYTES+1)
            if len(raw)>MAX_TASK_BYTES:raise EvidenceError('Refresh task exceeds bounded metadata size')
            Path(entry.path).chmod(0o600)
            task=json.loads(raw)
            protocol=WEEKLY if weekly else DAILY
            if task.get('protocol')!=protocol:raise EvidenceError('Report refresh task protocol mismatch')
            allowed={'protocol','experiment_id','event_id','observation_id','checksum'}|({'window_start_day','window_end_day','source_daily_revision'}if weekly else{'reporting_day'})
            if set(task)!=allowed:raise EvidenceError('Refresh tasks contain receipt metadata only')
            if task.get('experiment_id')not in experiments:continue
            for key in ('event_id','observation_id','checksum'):
                if not isinstance(task.get(key),str)or not task[key]:raise EvidenceError('Refresh task receipt identity missing')
            day=task.get('window_end_day')if weekly else task.get('reporting_day')
            date.fromisoformat(day)
            if weekly and date.fromisoformat(task['window_start_day'])!=date.fromisoformat(day)-timedelta(days=6):raise EvidenceError('Weekly repair window must be seven calendar days')
            if len(selected)<MAX_TASKS:selected.append({'path':Path(entry.path),'raw':raw,'task':task})
    selected.sort(key=lambda r:(r['task'].get('window_end_day',r['task'].get('reporting_day')),r['task']['event_id']))
    return selected,{'pending_files':total,'selected_tasks':len(selected),'selection_bound':MAX_TASKS,'overflow_retained':total>MAX_TASKS or total>MAX_PENDING_FILES,'pending_count_is_lower_bound':total>MAX_PENDING_FILES,'directory_scan_bound':MAX_PENDING_FILES}


def verify_receipts(evidence,tasks):
    receipts={}
    for record in tasks:
        task=record['task'];oid=task['observation_id']
        row=evidence.repo.require('portfolio_observation',oid).doc
        if row.get('experiment_id')!=task['experiment_id']or row.get('checksum')!=task['checksum']:raise EvidenceError('Refresh receipt strong metadata identity/checksum mismatch')
        kind=str(row.get('kind',''))
        inference=kind.startswith('managed_inference_evidence:')
        issue=kind.startswith('managed_inference_issue_link:')
        if kind!='managed_model_cost'and not kind.startswith('managed_model_cost:')and not inference and not issue:raise EvidenceError('Refresh receipt is not an immutable managed cost/inference event')
        raw=evidence.read_artifact_bytes(row)
        if checksum(raw)!=task['checksum']:raise EvidenceError('Refresh receipt original artifact checksum mismatch')
        payload=json.loads(raw)
        expected='finplan-managed-inference-issue-link/1'if issue else'finplan-managed-inference-evidence/1'if inference else'finplan-managed-model-cost/1'
        if payload.get('protocol',payload.get('schema_version'))!=expected:raise EvidenceError('Refresh receipt event protocol mismatch')
        day=timestamp(row['slot']).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        if payload.get('event_id')!=task['event_id']or payload.get('experiment_id')!=task['experiment_id']or timestamp(payload['issued_at']if issue else payload['occurred_at'])!=timestamp(row['slot']):raise EvidenceError('Refresh receipt event binding mismatch')
        if task['protocol']==DAILY and day!=task['reporting_day']:raise EvidenceError('Refresh receipt actual occurrence day mismatch')
        if task['protocol']==WEEKLY and not task['window_start_day']<=day<=task['window_end_day']:raise EvidenceError('Refresh receipt is outside requested weekly window')
        if oid in receipts and receipts[oid]!=row:raise EvidenceError('Conflicting strong observation receipt')
        receipts[oid]=row
        record['receipt']={**row,'payload':payload}
    return [record['receipt']for record in tasks]


def ensure_in_report(report,tasks):
    events={e['event_id']:e for field in('managed_cost_events','managed_inference_events','managed_inference_issue_links')for e in report.get(field,[])}
    for record in tasks:
        task=record['task'];event=events.get(task['event_id'])
        if not event or not event.get('counted_in_reporting_window')or not any(s.get('observation_id')==task['observation_id']and s.get('checksum')==task['checksum']for s in event['evidence_sources']):
            raise EvidenceError('Requested exact managed cost/inference receipt is not present in archived report')


def acknowledge(tasks):
    changed=set()
    for record in tasks:
        path=record['path']
        if path.is_symlink():raise EvidenceError('Refresh task changed to a symbolic link')
        # Concurrent replacement/new receipts survive. Queue producers publish
        # immutable exclusive files; only the exact read task may be acknowledged.
        try:
            if path.read_bytes()==record['raw']:
                path.unlink();changed.add(path.parent)
        except FileNotFoundError:pass
    for parent in changed:
        fd=os.open(parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)


def put_task(path,task):
    path=directory(path)
    fd=os.open(path/'queue.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    os.fchmod(fd,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        return _put_task_locked(path,task)
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


def _put_task_locked(path,task):
    raw=json.dumps(task,sort_keys=True,separators=(',',':')).encode()
    if len(raw)>MAX_TASK_BYTES:raise EvidenceError('Refresh task exceeds bounded metadata size')
    target=path/(hashlib.sha256(raw).hexdigest()+'.json')
    if not target.exists():
        with os.scandir(path) as entries:
            if sum(1 for item in entries if NAME.fullmatch(item.name))>=MAX_PENDING_FILES:
                raise EvidenceError('Weekly report refresh queue full; daily receipt remains pending')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path,prefix='.tmp-',delete=False)as stream:
            temporary=Path(stream.name);os.fchmod(stream.fileno(),0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,target)
        except FileExistsError:
            if target.is_symlink()or target.read_bytes()!=raw:raise EvidenceError('Permanent refresh task conflicts with retry')
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return target


def enqueue_existing_weekly(tasks,*,queue_dir,output_dir,daily_revision,mode='forward'):
    weekly=directory(Path(queue_dir)/'weekly');created=[]
    output=Path(output_dir)/('replay'if mode=='controlled_replay'else'')/'weekly'
    for record in tasks:
        task=record['task'];day=date.fromisoformat(task['reporting_day'])
        for offset in range(7):
            end=day+timedelta(days=offset);start=end-timedelta(days=6)
            # Seven deterministic alias paths; never scan growing report archives.
            if not (output/(start.isoformat()+'_'+end.isoformat()+'.json')).exists():continue
            repair={'protocol':WEEKLY,'experiment_id':task['experiment_id'],'window_start_day':start.isoformat(),'window_end_day':end.isoformat(),
                    'event_id':task['event_id'],'observation_id':task['observation_id'],'checksum':task['checksum'],'source_daily_revision':daily_revision}
            created.append(put_task(weekly,repair))
    return created
