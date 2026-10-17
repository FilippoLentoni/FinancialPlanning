import io
import json
from datetime import UTC, date, datetime
from zipfile import ZipFile

import pytest

from finplan_platform.providers.gdelt_metadata import HEADER, aggregate, freeze_day, freeze_rights


def archive(rows, day=date(2025,10,16)):
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as output:
        text = '\t'.join(HEADER)+'\n'
        for organizations, count, tone in rows:
            fields = [day.strftime('%Y%m%d'),str(count),'','','','',organizations,str(tone),'','','']
            text += '\t'.join(fields)+'\n'
        output.writestr(day.strftime('%Y%m%d.gkg.csv'), text)
    return stream.getvalue()


def test_weighted_tone_exact_entities_and_zero_match_coverage():
    body=archive([('apple inc;google;',2,10),('Apple Inc.;',1,-5),('applebee;metals company;amazon rainforest;',100,90)])
    result=aggregate(body,date(2025,10,16),('AAPL','META','GOOGL'))
    apple,meta,google=result['features']
    assert apple['article_count']==3 and apple['tone']==5
    assert google['article_count']==2 and google['tone']==10
    assert meta['article_count']==0 and meta['tone'] is None and meta['missing']
    assert result['row_count']==3


@pytest.mark.parametrize('count,tone',[(0,0),(1,float('nan')),(1,101)])
def test_invalid_archive_rows_block_complete_coverage(count,tone):
    with pytest.raises(ValueError,match='quality'):
        aggregate(archive([('google',count,tone)]),date(2025,10,16))


def test_freeze_separates_historical_provider_and_actual_platform_availability(tmp_path):
    body=archive([('google',1,-2)])
    class Response(io.BytesIO):
        headers={'Last-Modified':'Fri, 17 Oct 2025 07:01:06 GMT','ETag':'synthetic'}
        def geturl(self):return 'https://data.gdeltproject.org/gkg/20251016.gkg.csv.zip'
    def opener(*args,**kwargs):return Response(body)
    first=freeze_day(date(2025,10,16),tmp_path,symbols=('GOOGL',),opener=opener)
    assert first['source_available_at']=='2025-10-17T12:00:00Z'
    source=json.loads((tmp_path/'2025/10/16/source.json').read_text())
    assert source['platform_available_at']==source['retrieved_at']
    assert datetime.fromisoformat(source['retrieved_at'].replace('Z','+00:00'))>datetime(2025,10,17,tzinfo=UTC)
    second=freeze_day(date(2025,10,16),tmp_path,symbols=('GOOGL',),opener=lambda *a,**k:pytest.fail('immutable retry downloaded'))
    assert first==second
    (tmp_path/'2025/10/16/archive.zip').write_bytes(b'changed')
    with pytest.raises(ValueError,match='checksum'):freeze_day(date(2025,10,16),tmp_path,symbols=('GOOGL',))


def test_rights_evidence_is_scoped_to_released_metadata_and_checksum_verified(tmp_path):
    body=b'<html>unlimited and unrestricted use for academic, commercial use; include a citation</html>'
    evidence=freeze_rights(tmp_path,opener=lambda *a,**k:io.BytesIO(body))
    assert evidence['rights_status']=='research_and_training'
    assert evidence['scope']=='released_GDELT_machine_metadata_only'
    assert freeze_rights(tmp_path,opener=lambda *a,**k:pytest.fail('download'))==evidence


def test_cloud_capture_checks_project_budget_on_weekends_too(make_deps,monkeypatch):
    from finplan_platform.core import gdelt_metadata as producer
    from finplan_platform.core.clock import FrozenClock
    from finplan_platform.core.calendar import xnys_calendar
    from finplan_platform.core.errors import PlatformError
    deps=make_deps(calendar=xnys_calendar())
    monkeypatch.setattr(producer,'freeze_rights',lambda *a,**k:pytest.fail('provider call'))
    def denied(ctx):raise PlatformError('BUDGET_EXCEEDED','synthetic budget limit')
    deps.budget_gate=denied
    with pytest.raises(PlatformError,match='budget'):
        producer.capture({'source':'finplan.gdelt-metadata'},deps=deps,clock=FrozenClock(datetime(2026,10,17,13,tzinfo=UTC)))
    with pytest.raises(PlatformError,match='budget'):
        producer.capture({'source':'finplan.gdelt-metadata','source_day':'2025-10-16'},deps=deps,clock=FrozenClock(datetime(2026,10,17,13,tzinfo=UTC)))


def test_cloud_producer_freezes_raw_curated_rights_and_repairs_no_duplicate_purchase(make_deps,monkeypatch):
    from pathlib import Path
    from finplan_platform.core import gdelt_metadata as producer
    from finplan_platform.core.clock import FrozenClock
    from finplan_platform.providers.gdelt_metadata import canonical,checksum,FEATURE_VERSION
    deps=make_deps();now=datetime(2026,10,17,13,tzinfo=UTC);raw=archive([('google',1,-2)])
    terms=b'Synthetic GDELT metadata rights evidence'
    def rights(root):
        (root/'rights-evidence.html').write_bytes(terms)
        return {'evidence_checksum':checksum(terms)}
    def source(day,root):
        base=root/day.strftime('%Y/%m/%d');base.mkdir(parents=True)
        (base/'archive.zip').write_bytes(raw)
        document=aggregate(raw,day)
        document['lineage']={'source_available_at':'2025-10-17T12:00:00Z','retrieved_at':now.isoformat(),
                             'archive_checksum':checksum(raw)}
        output=base/(FEATURE_VERSION+'.json');output.write_bytes(canonical(document))
        return {'feature_path':str(output),'archive_checksum':checksum(raw),'archive_bytes':len(raw)}
    monkeypatch.setattr(producer,'freeze_rights',rights);monkeypatch.setattr(producer,'freeze_day',source)
    event={'source':'finplan.gdelt-metadata','source_day':'2025-10-16'}
    result=producer.capture(event,deps=deps,clock=FrozenClock(now))
    assert result['status']=='captured' and result['central_available_at']=='2026-10-17T13:00:00Z'
    assert len(list(deps.store.list_objects('raw',producer.PREFIX)))==2
    assert len(list(deps.store.list_objects('curated',producer.PREFIX)))==1
    monkeypatch.setattr(producer,'freeze_day',lambda *a,**k:pytest.fail('duplicate provider request'))
    assert producer.capture(event,deps=deps,clock=FrozenClock(now))['status']=='already_frozen'
    record_id=checksum(canonical({'source_day':'2025-10-16','archive_checksum':checksum(raw)}))
    assert len(deps.repo.audit_events(record_id))==1
