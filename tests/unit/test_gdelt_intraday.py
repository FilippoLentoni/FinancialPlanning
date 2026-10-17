from datetime import UTC,datetime
import json
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate
from finplan_platform.core.gdelt_intraday import PREFIX,read
from finplan_platform.providers.gdelt_metadata import canonical,checksum
from tests.api_support import *

def test_intraday_route_preserves_document_identity_and_explicit_availability(svc,clients,clock):
    clock.set('2026-01-03T14:00:00Z')
    document=json.loads((load_store().fixtures_dir('gdelt-intraday-document')/'valid/synthetic.json').read_text())
    body=canonical(document)
    svc.store.put_once('curated',PREFIX+'2026/01/02/'+checksum(body).split(':')[1]+'.json',body)
    path='/v1/gdelt-intraday-metadata?symbols=GOOGL,VOO&start_date=2026-01-02&end_date=2026-01-02'
    for client in (clients.reader,clients.fm_job,clients.fm_job_api):
        status,result,_=client.get(path)
        assert status==200,result
        assert validate(result,'api/gdelt-intraday-response').valid
        assert result['documents'][0]['checksum']==checksum(body)
    status,result,_=clients.fm_job.get(path+'&decision_at=2026-01-02T13:45:00Z')
    assert status==200 and not result['documents'] # Not centrally present at historical time.
    status,result,_=clients.fm_job.get(path+'&decision_at=2026-01-02T13:45:00Z&availability_mode=historical_source_proxy')
    assert status==200 and len(result['documents'])==1
    assert clients.reader.post('/v1/gdelt-intraday-metadata',{})[0] in (403,404,405)
