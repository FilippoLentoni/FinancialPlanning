from datetime import UTC, datetime, timedelta
import json

import pytest

from tests.api_support import *
from finplan_platform.core.clock import FrozenClock
from finplan_platform.core.context import Caller, OperationContext
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.gdelt_metadata import PREFIX, read
from finplan_platform.providers.gdelt_metadata import FEATURE_VERSION, canonical, checksum


def context(now):
    return OperationContext(caller=Caller(principal='synthetic-model'),env='beta',clock=FrozenClock(now),correlation_id='synthetic-gdelt')


def store_document(svc, *, source_available, central_available, day='2025-10-16'):
    document={'feature_version':FEATURE_VERSION,'source_day':day,'row_count':1,'rejected_rows':0,
              'features':[{'instrument_id':'GOOGL','article_count':1,'cluster_count':1,'tone':-2.,'missing':False}],
              'aliases':{'GOOGL':['google']},'disclosures':['Synthetic machine metadata.'],
              'lineage':{'source_available_at':source_available,'central_available_at':central_available,
                         'platform_available_at':central_available,'retrieved_at':central_available}}
    body=canonical(document)
    key=PREFIX+day.replace('-','/')+'/'+checksum(body).split(':')[1]+'.json'
    svc.store.put_once('curated',key,body)
    return document


def test_historical_source_proxy_is_explicit_and_cannot_claim_platform_availability(svc):
    now=datetime(2026,10,17,14,tzinfo=UTC)
    store_document(svc,source_available='2025-10-17T12:00:00Z',central_available=now.isoformat())
    query={'symbols':'GOOGL','start_date':'2025-10-16','end_date':'2025-10-16','decision_at':'2025-10-17T13:00:00Z'}
    platform=read(context(now),svc,query)
    assert not platform['documents'] and platform['coverage']['missing_days']==['2025-10-16']
    proxy=read(context(now),svc,{**query,'availability_mode':'historical_source_proxy'})
    assert proxy['coverage']['complete'] and len(proxy['documents'])==1
    item=proxy['documents'][0]
    assert item['checksum']==checksum(canonical(item['document']))
    assert item['document']['lineage']['central_available_at']==now.isoformat()


def test_later_provider_modified_archive_is_unavailable_to_old_decision(svc):
    now=datetime(2026,10,17,14,tzinfo=UTC)
    store_document(svc,source_available='2026-10-17T12:00:00Z',central_available=now.isoformat())
    result=read(context(now),svc,{'symbols':'GOOGL','start_date':'2025-10-16','end_date':'2025-10-16',
                                'decision_at':'2025-10-17T13:00:00Z','availability_mode':'historical_source_proxy'})
    assert not result['documents'] and not result['coverage']['complete']


@pytest.mark.parametrize('change',[{'end_date':'2025-12-01'},{'symbols':'GOOGL,GOOGL'},{'symbols':'UNSUPPORTED'},
                                   {'availability_mode':'invented'},{'decision_at':'2027-01-01T00:00:00Z'}])
def test_source_read_bounds_and_cutoff_are_validated(svc,change):
    now=datetime(2026,10,17,14,tzinfo=UTC)
    query={'symbols':'GOOGL','start_date':'2025-10-16','end_date':'2025-10-16'}
    with pytest.raises(PlatformError) as error:read(context(now),svc,{**query,**change})
    assert error.value.code=='VALIDATION_FAILED'


def test_numeric_route_validates_contract_and_allows_model_reader_without_writes(svc,clients,clock):
    from finplan_contracts.schemas import load_store
    from finplan_contracts.validate import validate
    clock.set('2026-01-03T14:00:00Z')
    document=json.loads((load_store().fixtures_dir('gdelt-metadata-document')/'valid/synthetic.json').read_text())
    body=canonical(document)
    svc.store.put_once('curated',PREFIX+'2026/01/01/'+checksum(body).split(':')[1]+'.json',body)
    path='/v1/gdelt-metadata?symbols=GOOGL,VOO&start_date=2026-01-01&end_date=2026-01-01'
    for client in (clients.reader,clients.fm_job,clients.fm_job_api):
        status,response,_=client.get(path)
        assert status==200,response
        assert response['coverage']['complete']
        assert validate(response,'api/gdelt-metadata-response').valid
        assert response['documents'][0]['checksum']==checksum(body)
    status,_,_=clients.reader.post('/v1/gdelt-metadata',{})
    assert status in (403,404,405)
