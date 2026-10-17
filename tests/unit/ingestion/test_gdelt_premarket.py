import io
import json
from datetime import UTC,date,datetime,timedelta
from urllib.error import HTTPError
from zipfile import ZipFile
import pytest
from finplan_platform.providers.gdelt_premarket import aggregate,freeze_day,window

def zip_body(batch,tone=2,org=b'google',identifier=b'https://synthetic.invalid/a'):
    name=batch.astimezone(UTC).strftime('%Y%m%d%H%M%S')
    fields=[b'']*27;fields[0]=(name+'-0').encode();fields[1]=name.encode();fields[2]=b'1';fields[4]=identifier;fields[13]=org;fields[15]=str(tone).encode()+b',0,0'
    fields[26]=b'legacy\xe9' # Untouched source metadata must not corrupt selected ASCII fields.
    data=io.BytesIO()
    with ZipFile(data,'w') as z:z.writestr(name+'.gkg.csv',b'\t'.join(fields)+b'\n')
    return data.getvalue()

def opener(day,missing=()):
    batches=[window(day)[0]+timedelta(minutes=i) for i in (0,15,30,45)]
    bodies={b.astimezone(UTC).strftime('%Y%m%d%H%M%S'):zip_body(b) for b in batches}
    class Response(io.BytesIO):
        headers={'Last-Modified':'Thu, 02 Jan 2025 12:30:00 GMT','ETag':'synthetic'}
        def __init__(self,body,url):super().__init__(body);self.url=url
        def geturl(self):return self.url
    def fetch(request,**kwargs):
        name=request.full_url.rsplit('/',1)[1].split('.')[0]
        if name in [batches[i].astimezone(UTC).strftime('%Y%m%d%H%M%S') for i in missing]:
            raise HTTPError(request.full_url,404,'missing',{},None)
        return Response(bodies[name],request.full_url)
    return fetch

def rights(root):
    root.mkdir(exist_ok=True);(root/'rights-evidence.json').write_text(json.dumps({'rights_status':'research_and_training'}))

@pytest.mark.parametrize('missing,quality,captured',[((), 'complete',4),((1,), 'partial',3),((0,1,2,3),'unavailable',0)])
def test_source_coverage_distinguishes_known_zero_from_unavailable(tmp_path,missing,quality,captured):
    day=date(2025,1,2);rights(tmp_path)
    doc=freeze_day(day,tmp_path,opener=opener(day,missing),now=datetime(2026,10,17,tzinfo=UTC))
    assert doc['quality_status']==quality and doc['captured_batches']==captured
    assert len(doc['lineage']['attempts'])==4 and len(doc['missing_batches'])==4-captured
    apple=next(f for f in doc['features'] if f['instrument_id']=='AAPL')
    assert apple['article_count']==(0 if captured else None)
    assert apple['tone'] is None and apple['missing']==(captured<4)
    google=doc['features'][0]
    assert google['article_count']==(1 if captured else None) # Deduplicated across all successful batches.
    assert doc['lineage']['source_available_at'] is not None if captured else doc['lineage']['source_available_at'] is None
    assert freeze_day(day,tmp_path,opener=lambda *a,**k:pytest.fail('duplicate download'))==doc

def test_conservative_batch_availability_and_dst(tmp_path):
    day=date(2025,1,2);rights(tmp_path)
    doc=freeze_day(day,tmp_path,opener=opener(day),now=datetime(2026,10,17,tzinfo=UTC))
    assert doc['lineage']['source_available_at']=='2025-01-02T13:45:00Z'
    assert doc['decision_cutoff']=='2025-01-02T14:00:00Z'
    assert window(date(2026,10,16))[2].astimezone(UTC).hour==13
    assert doc['lineage']['retrieved_at'].startswith('2026-10-17')

def test_invalid_numeric_source_cannot_be_imputed_to_zero():
    batch=window(date(2025,1,2))[0]
    with pytest.raises(ValueError,match='tone'):aggregate(zip_body(batch,float('nan')),batch)

def test_budget_denies_intraday_before_any_provider_call(make_deps,monkeypatch):
    from finplan_platform.core import gdelt_intraday
    from finplan_platform.core.clock import FrozenClock
    from finplan_platform.core.errors import PlatformError
    deps=make_deps()
    monkeypatch.setattr(gdelt_intraday,'freeze_rights',lambda *a,**k:pytest.fail('source call before budget'))
    def deny(ctx):raise PlatformError('BUDGET_EXCEEDED','synthetic')
    deps.budget_gate=deny
    with pytest.raises(PlatformError):gdelt_intraday.capture({'source_day':'2025-01-02'},deps=deps,clock=FrozenClock(datetime(2026,10,17,14,tzinfo=UTC)))

def test_central_intraday_retains_sources_and_immutable_retry_audit(make_deps,monkeypatch):
    from finplan_platform.core import gdelt_intraday as producer
    from finplan_platform.core.clock import FrozenClock
    from finplan_platform.providers.gdelt_metadata import checksum,canonical
    deps=make_deps();day=date(2025,1,2);now=datetime(2026,10,17,14,tzinfo=UTC)
    def rights(root):
        root.mkdir(exist_ok=True);terms=b'Synthetic terms'
        (root/'rights-evidence.html').write_bytes(terms)
        (root/'rights-evidence.json').write_text(json.dumps({'evidence_checksum':checksum(terms)}))
        return {'evidence_checksum':checksum(terms)}
    def capture(day,root,**kwargs):return freeze_day(day,root,opener=opener(day),now=now)
    monkeypatch.setattr(producer,'freeze_rights',rights);monkeypatch.setattr(producer,'freeze_day',capture)
    result=producer.capture({'source_day':str(day)},deps=deps,clock=FrozenClock(now))
    assert result['status']=='captured' and result['archives_fetched']==4
    assert len(list(deps.store.list_objects('raw',producer.PREFIX)))==5
    assert len(list(deps.store.list_objects('curated',producer.PREFIX)))==1
    monkeypatch.setattr(producer,'freeze_day',lambda *a,**k:pytest.fail('duplicate provider call'))
    assert producer.capture({'source_day':str(day)},deps=deps,clock=FrozenClock(now))['status']=='already_frozen'
    assert len(deps.repo.audit_events(result['feature_checksum']))==1
