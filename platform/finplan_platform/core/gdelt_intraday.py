"""Producer-owned versioned premarket GKG2 metadata; no caller data writes."""
from datetime import UTC,date,datetime
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from ..providers.gdelt_metadata import canonical,checksum,freeze_rights,stamp
from ..providers.gdelt_premarket import FEATURE_VERSION,freeze_day,window
from .context import Caller,OperationContext
from .errors import PlatformError
from .gdelt_metadata import _require_beta

PREFIX='gdelt-intraday/v1/'

def _audit(ctx,deps,doc):
    from .audit import audit_event
    digest=checksum(canonical(doc))
    if not deps.repo.audit_events(digest):
        deps.repo.commit([],audit=[audit_event(ctx,record_id=digest,record_type='research_metadata',operation='capture_gdelt_intraday',new={'source_day':doc['source_day'],'feature_version':FEATURE_VERSION},feature_checksum=digest,source_available_at=doc['lineage']['source_available_at'],central_available_at=doc['lineage']['central_available_at'])])

def capture(event,*,deps=None,clock=None):
    from .clock import SystemClock
    from .ingestion import default_deps
    from .audit import audit_event
    deps=deps or default_deps();clock=clock or SystemClock();_require_beta(deps.config.env)
    now=clock.now()
    expected=event.get("expected_sources", {})
    if not isinstance(expected,dict) or len(expected)>4 or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch(r"sha256:[a-f0-9]{64}",v) for k,v in expected.items()):
        raise PlatformError.validation("expected_sources must contain at most four source checksums")
    try:
        day=date.fromisoformat(event['source_day']) if event.get('source_day') else now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date()
        if now < window(day)[1]:raise ValueError('incomplete premarket window')
    except (ValueError,TypeError):
        raise PlatformError.validation('source_day must have a completed premarket window') from None
    ctx=OperationContext(caller=Caller(principal=f'finplan-{deps.config.env}-financialplanning-ingestion-handler'),env=deps.config.env,correlation_id='gdelt2_'+day.strftime('%Y%m%d'),clock=clock,trigger='on_demand' if event.get('source_day') else 'scheduled')
    if deps.budget_gate is not None:deps.budget_gate(ctx)
    prefix=PREFIX+day.strftime('%Y/%m/%d/')
    existing=list(deps.store.list_objects('curated',prefix))
    if existing:
        raw,_=deps.store.get('curated',existing[0][0]);doc=json.loads(raw)
        if doc.get('source_day')!=str(day) or doc.get('feature_version')!=FEATURE_VERSION or checksum(raw).split(':')[1]!=Path(existing[0][0]).stem:
            raise PlatformError.internal('frozen GKG2 window identity failed')
        actual={s["batch_at"]:s["archive_checksum"] for s in doc["lineage"]["sources"]}
        if any(actual.get(k)!=v for k,v in expected.items()):
            raise PlatformError.precondition("central GKG2 vintage differs from frozen expected source")
        _audit(ctx,deps,doc)
        return {'status':'already_frozen','source_day':str(day),'quality_status':doc['quality_status'],'archives_fetched':0}
    with TemporaryDirectory(prefix='gdelt-intraday-') as temporary:
        root=Path(temporary);rights=freeze_rights(root);doc=freeze_day(day,root,now=now)
        actual={s["batch_at"]:s["archive_checksum"] for s in doc["lineage"]["sources"]}
        if any(actual.get(k)!=v for k,v in expected.items()):
            raise PlatformError.precondition("GKG2 provider vintage differs from frozen expected source")
        base=root/day.strftime('%Y/%m/%d')
        for source in doc['lineage']['sources']:
            name=datetime.fromisoformat(source['batch_at'].replace('Z','+00:00')).strftime('%Y%m%d%H%M%S')
            deps.store.put_once_or_verify('raw',prefix+source['archive_checksum'].split(':')[1]+'.zip',(base/(name+'.zip')).read_bytes(),'application/zip')
            quarantine=base/(name+'.quality.json')
            if quarantine.exists():
                deps.store.put_once_or_verify('raw',prefix+source['archive_checksum'].split(':')[1]+'.quality.json',quarantine.read_bytes())
        deps.store.put_once_or_verify('raw',PREFIX+'rights/'+rights['evidence_checksum'].split(':')[1]+'.html',(root/'rights-evidence.html').read_bytes(),'text/html')
        doc['lineage']['central_available_at']=stamp(clock.now());body=canonical(doc);digest=checksum(body)
        deps.store.put_once_or_verify('curated',prefix+digest.split(':')[1]+'.json',body)
        _audit(ctx,deps,doc)
    return {'status':'captured','source_day':str(day),'quality_status':doc['quality_status'],'row_count':doc['row_count'],'rejected_rows':doc['rejected_rows'],'archives_fetched':doc['captured_batches'],'feature_version':FEATURE_VERSION,'feature_checksum':digest,'archive_bytes':sum(s['archive_bytes'] for s in doc['lineage']['sources']),'source_available_at':doc['lineage']['source_available_at'],'central_available_at':doc['lineage']['central_available_at']}

def read(ctx,svc,query):
    from .gdelt_metadata import read as read_numeric
    return read_numeric(ctx,svc,query,prefix=PREFIX,feature_version=FEATURE_VERSION,request_schema='api/gdelt-intraday-request')
