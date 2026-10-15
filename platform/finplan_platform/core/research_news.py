"""Immutable paid search metadata; source text is data and never instructions."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
from urllib.parse import urlparse
from .artifacts import ArtifactNotFound
from .errors import PlatformError
from .upgrade import current_version
from .contract_io import require_valid

PREFIX = 'research-news/v1/'


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def checksum(value):
    return 'sha256:'+hashlib.sha256(canonical(value)).hexdigest()


def dt(value):
    out=datetime.fromisoformat(value.replace('Z','+00:00'))
    if out.tzinfo is None:raise ValueError('timezone required')
    return out.astimezone(UTC)


def stamp(value):return value.astimezone(UTC).isoformat().replace('+00:00','Z')


def require_beta(svc):
    if svc.env!='beta':raise PlatformError('OPERATION_NOT_PERMITTED','news research is beta only')


def eligibility(body):
    reasons=[]
    rights=body['rights']
    if rights['status'] not in ('research_only','research_and_training') or not rights['evidence_url'] or not rights['evidence_checksum']:
        reasons.append('research_rights_unverified')
    if rights['scope']!='search_metadata':reasons.append('unsupported_content_scope')
    if body['quality_status']!='validated':reasons.append('quality_not_validated')
    if not body['payload']['records']:reasons.append('empty_delivery')
    for row in body['payload']['records']:
        if row['published_at'] is None:reasons.append('publication_time_unverified');continue
        if dt(row['published_at'])>dt(body['retrieved_at']):reasons.append('publication_after_retrieval')
        if urlparse(row['url']).scheme!='https':reasons.append('unsafe_source_url')
        if not set(row['symbols'])<=set(body['symbols']):reasons.append('symbol_scope_mismatch')
    return sorted(set(reasons))


def audit_import(ctx,svc,manifest):
    from .audit import audit_event
    if not svc.repo.audit_events(manifest['record_id']):
        svc.repo.commit([],audit=[audit_event(ctx,record_id=manifest['record_id'],record_type='research_news',operation='import_research_news',
            new={'eligible':manifest['eligible']},payload_checksum=manifest['payload_checksum'],receipt_checksum=manifest['receipt_checksum'])])


def ingest(ctx,svc,body):
    require_beta(svc)
    require_valid(body,'api/research-news-import-request')
    if checksum(body['payload'])!=body['payload_checksum'] or checksum(body['payment_receipt'])!=body['receipt_checksum']:
        raise PlatformError('VALIDATION_FAILED','news payload or receipt checksum mismatch')
    try:
        retrieved=dt(body['retrieved_at'])
        if retrieved>ctx.clock.now():raise ValueError('future retrieval')
        reasons=eligibility(body)
    except (TypeError,ValueError):
        raise PlatformError('VALIDATION_FAILED','invalid or future news timestamp') from None
    content={k:v for k,v in body.items() if k!='contract_version'}
    identity=checksum(content);digest=identity.split(':')[1]
    rawkey=PREFIX+'deliveries/'+digest+'.json'
    # Freeze raw before manifest; retry after a partial commit repairs the manifest.
    svc.store.put_once_or_verify('raw',rawkey,canonical(content))
    manifestkey=PREFIX+'manifests/'+retrieved.strftime('%Y/%m/%d/')+digest+'.json'
    try:
        frozen,_=svc.store.get('curated',manifestkey)
        manifest=json.loads(frozen);audit_import(ctx,svc,manifest)
        return {'manifest':manifest,'contract_version':current_version()}
    except ArtifactNotFound:pass
    manifest={'record_id':identity,'provider':body['provider'],'query':body['query'],'symbols':body['symbols'],
              'retrieved_at':body['retrieved_at'],'available_at':stamp(ctx.clock.now()),'payload_checksum':body['payload_checksum'],
              'receipt_checksum':body['receipt_checksum'],'rights':body['rights'],'quality_status':body['quality_status'],
              'eligible':not reasons,'exclusion_reasons':reasons,'records':body['payload']['records']}
    svc.store.put_once_or_verify('curated',manifestkey,canonical(manifest))
    audit_import(ctx,svc,manifest)
    return {'manifest':manifest,'contract_version':current_version()}


def latest(ctx,svc,query):
    require_beta(svc)
    try:
        symbols=query.get('symbols','SPY,VOO').split(',')
        if not 1<=len(symbols)<=5 or len(set(symbols))!=len(symbols):raise ValueError('symbol bound')
        import re
        if any(not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,9}',s) for s in symbols):raise ValueError('symbol')
        decision=dt(query['decision_at']) if query.get('decision_at') else ctx.clock.now()
        if decision>ctx.clock.now():raise ValueError('future decision')
    except (TypeError,ValueError):raise PlatformError('VALIDATION_FAILED','invalid news query') from None
    rows=[];scanned=0
    # Strict seven-day query and scan bounds prevent unbounded catalog reads.
    for day in range(7):
        prefix=PREFIX+'manifests/'+(decision-timedelta(days=day)).strftime('%Y/%m/%d/')
        for key,_ in svc.store.list_objects('curated',prefix):
            scanned+=1
            if scanned>336 or len(rows)>=168:raise PlatformError('PRECONDITION_FAILED','news query exceeds bounded seven-day delivery window')
            raw,_=svc.store.get('curated',key);doc=json.loads(raw)
            if not set(symbols)&set(doc['symbols']) or dt(doc['available_at'])>decision:continue
            if decision-dt(doc['retrieved_at'])>timedelta(days=7):continue
            rows.append(doc)
    rows.sort(key=lambda row:(row['available_at'],row['record_id']))
    return {'decision_at':stamp(decision),'symbols':symbols,'deliveries':rows,'historical_coverage':'forward_collection_only','contract_version':current_version()}
