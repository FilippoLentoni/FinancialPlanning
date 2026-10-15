import copy,json
from datetime import timedelta
import pytest
from tests.api_support import *
from finplan_platform.core.research_news import ingest,latest,checksum
from finplan_platform.core.context import Caller,OperationContext
from finplan_platform.core.clock import FrozenClock


def body(now):
    payload={'records':[{'title':'Synthetic ETF growth','url':'https://example.invalid/news','published_at':(now-timedelta(hours=2)).isoformat(),'symbols':['SPY']}]}
    receipt={'settlement_status':'success','amount_usdc':.011,'transaction_id':'synthetic-tx'}
    return {'provider':'synthetic','query':'synthetic ETF','symbols':['SPY'],'retrieved_at':(now-timedelta(hours=1)).isoformat(),'payload_checksum':checksum(payload),'receipt_checksum':checksum(receipt),'payment_receipt':receipt,'payload':payload,'rights':{'status':'unverified','scope':'search_metadata','evidence_url':'','evidence_checksum':None},'quality_status':'validated'}


def test_import_replay_quarantine_and_pit(svc):
    from datetime import datetime,UTC
    now=datetime(2026,1,2,14,tzinfo=UTC);ctx=OperationContext(caller=Caller(principal='synthetic'),env='beta',clock=FrozenClock(now),correlation_id='synthetic-news')
    b=body(now);first=ingest(ctx,svc,b);second=ingest(ctx,svc,b)
    assert first==second and not first['manifest']['eligible']
    assert 'research_rights_unverified' in first['manifest']['exclusion_reasons']
    assert len(svc.repo.audit_events(first['manifest']['record_id']))==1
    assert len(latest(ctx,svc,{'symbols':'SPY'})['deliveries'])==1
    assert not latest(ctx,svc,{'symbols':'SPY','decision_at':(now-timedelta(minutes=1)).isoformat()})['deliveries']
    bad=copy.deepcopy(b);bad['payload']['records'][0]['title']='tampered'
    with pytest.raises(Exception):ingest(ctx,svc,bad)


def test_eligible_and_unverifiable_dates(svc):
    from datetime import datetime,UTC
    now=datetime(2026,1,2,14,tzinfo=UTC);ctx=OperationContext(caller=Caller(principal='synthetic'),env='beta',clock=FrozenClock(now),correlation_id='synthetic-news')
    b=body(now);b['rights']={'status':'research_only','scope':'search_metadata','evidence_url':'https://example.invalid/terms','evidence_checksum':'sha256:'+'a'*64}
    assert ingest(ctx,svc,b)['manifest']['eligible']
    b['payload']['records'][0]['published_at']=None;b['payload_checksum']=checksum(b['payload'])
    assert not ingest(ctx,svc,b)['manifest']['eligible']


def test_total_manifest_scan_is_bounded_before_symbol_filter(svc):
    from datetime import datetime,UTC
    from types import SimpleNamespace
    from finplan_platform.core.research_news import canonical
    now=datetime(2026,1,2,14,tzinfo=UTC);ctx=OperationContext(caller=Caller(principal='synthetic'),env='beta',clock=FrozenClock(now),correlation_id='synthetic-news')
    original=ingest(ctx,svc,body(now))['manifest'];original['symbols']=['VOO'];scanned=[]
    class Store:
        def list_objects(self,*args):
            for i in range(337):yield str(i),now
        def get(self,role,key):scanned.append(key);return canonical(original),None
    fake=SimpleNamespace(env='beta',store=Store())
    with pytest.raises(Exception,match='bounded'):latest(ctx,fake,{'symbols':'SPY'})
    assert len(scanned)==336
