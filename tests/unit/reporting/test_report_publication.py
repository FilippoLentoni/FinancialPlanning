"""Concurrent/restarted writers never regress a period or mix archived exports."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import threading
import pytest
from finplan_platform.reporting import publication
from finplan_platform.reporting.reports import write_report, EvidenceError
from tests.unit.reporting.test_modeling_reports import report


def candidate(at, day=None):
    data=report();data['generated_at']=at;data['data_cutoff']=at
    if day:data['start_day']=data['end_day']=day
    return data


def assert_aliases(result, name='latest'):
    target=Path(result['directory']);archive=Path(result['archive'])
    for ext in publication.EXTENSIONS:
        assert (target/(name+'.'+ext)).read_bytes()==(archive/('report.'+ext)).read_bytes()


def test_concurrent_later_finishes_before_older_and_monotonic_heads(tmp_path,monkeypatch):
    old=candidate('2026-10-12T17:00:00Z');new=candidate('2026-10-12T18:00:00Z')
    held=threading.Event();release=threading.Event();original=publication.publish
    def delayed(target,archive,manifest,data,*args,**kwargs):
        if data['generated_at']==old['generated_at']:
            held.set();assert release.wait(10)
        return original(target,archive,manifest,data,*args,**kwargs)
    monkeypatch.setattr(publication,'publish',delayed)
    with ThreadPoolExecutor(2) as pool:
        older=pool.submit(write_report,old,tmp_path);assert held.wait(10)
        newer=pool.submit(write_report,new,tmp_path).result(timeout=10);release.set()
        skipped=older.result(timeout=10)
    assert skipped['aliases_published']==[]
    assert_aliases(newer);assert_aliases(newer,new['end_day'])
    assert Path(skipped['archive']).exists() # Old immutable evidence preserved.
    # A newer generated timestamp cannot replace a more recent data cutoff.
    regression=candidate('2026-10-12T19:00:00Z');regression['data_cutoff']='2026-10-12T17:59:00Z'
    assert write_report(regression,tmp_path)['aliases_published']==[]
    assert_aliases(newer)


def test_crash_between_exports_replays_one_verified_revision_before_older_skip(tmp_path,monkeypatch):
    old=candidate('2026-10-12T17:00:00Z');new=candidate('2026-10-12T18:00:00Z')
    first=write_report(old,tmp_path);original=publication._atomic;failed=False
    def crash(path,raw):
        nonlocal failed
        if path.name==new['end_day']+'.csv' and not failed:
            failed=True;raise OSError('controlled crash after HTML alias')
        return original(path,raw)
    monkeypatch.setattr(publication,'_atomic',crash)
    with pytest.raises(OSError,match='controlled crash'):write_report(new,tmp_path)
    target=Path(first['directory']);assert (target/'.report-publication.pending.json').exists()
    monkeypatch.setattr(publication,'_atomic',original)
    recovered=write_report(old,tmp_path)
    assert recovered['aliases_published']==[] and recovered['publication_recovered'] is True
    import json
    head=json.loads((target/'.report-heads/latest.json').read_bytes())
    result={'directory':str(target),'archive':str(target/head['archive'])}
    assert_aliases(result);assert_aliases(result,new['end_day'])
    assert head['generated_at']==new['generated_at']
    assert not (target/'.report-publication.pending.json').exists()


def test_historical_repair_and_older_period_never_regress_latest(tmp_path):
    current=write_report(candidate('2026-10-13T20:00:00Z','2026-10-13'),tmp_path)
    repaired=write_report(candidate('2026-10-14T20:00:00Z','2026-10-12'),tmp_path,update_latest=False)
    assert repaired['aliases_published']==['2026-10-12'];assert_aliases(current);assert_aliases(repaired,'2026-10-12')
    older=write_report(candidate('2026-10-15T20:00:00Z','2026-10-12'),tmp_path)
    assert older['aliases_published']==['2026-10-12'];assert_aliases(current)


def test_legacy_without_heads_migrates_from_verified_archive_without_regression(tmp_path):
    data=candidate('2026-10-12T18:00:00Z');latest=write_report(data,tmp_path)
    heads=Path(latest['directory'])/'.report-heads'
    for p in heads.iterdir():p.unlink()
    old=write_report(candidate('2026-10-12T17:00:00Z'),tmp_path)
    assert old['aliases_published']==[];assert_aliases(latest)
    # Losing the head cannot legitimize disagreeing legacy CSV/HTML/JSON.
    for p in heads.iterdir():p.unlink()
    (Path(latest['directory'])/'latest.csv').write_text('corrupt legacy alias')
    with pytest.raises(EvidenceError,match='Legacy alias exports disagree'):write_report(data,tmp_path)


def test_corrupt_recovery_archive_is_never_published(tmp_path,monkeypatch):
    data=candidate('2026-10-12T18:00:00Z');original=publication._finish
    monkeypatch.setattr(publication,'_finish',lambda *a:(_ for _ in ()).throw(OSError('controlled pre-publication crash')))
    with pytest.raises(OSError):write_report(data,tmp_path)
    target=tmp_path/'replay';import json
    pending=json.loads((target/'.report-publication.pending.json').read_bytes())
    archive=target/next(iter(pending['updates'].values()))['archive']
    (archive/'report.csv').write_text('corrupt')
    monkeypatch.setattr(publication,'_finish',original)
    with pytest.raises(EvidenceError,match='Immutable report publication artifact integrity'):write_report(candidate('2026-10-12T19:00:00Z'),tmp_path)
    assert not (target/'latest.html').exists()


def test_corrupt_head_dates_cannot_fake_newer_period(tmp_path):
    import json
    data=candidate('2026-10-12T18:00:00Z');result=write_report(data,tmp_path)
    path=Path(result['directory'])/'.report-heads/latest.json'
    head=json.loads(path.read_bytes());head['start_day']=head['end_day']='2099-12-31';path.write_text(json.dumps(head))
    with pytest.raises(EvidenceError,match='head dates/type disagree'):write_report(candidate('2026-10-12T19:00:00Z'),tmp_path)
    assert_aliases(result)


def test_archive_bytes_and_directories_are_fsynced_before_intent(tmp_path,monkeypatch):
    import os
    seen=[];original=os.fsync;publish=publication.publish
    def track(fd):
        seen.append(os.readlink('/proc/self/fd/'+str(fd)));return original(fd)
    def check(target,archive,*args,**kwargs):
        assert all(str(archive/('report.'+ext)) in seen for ext in publication.EXTENSIONS)
        assert str(archive/'manifest.json') in seen
        assert all(str(d) in seen for d in (archive,archive.parent,archive.parent.parent,target))
        return publish(target,archive,*args,**kwargs)
    monkeypatch.setattr(os,'fsync',track);monkeypatch.setattr(publication,'publish',check)
    write_report(candidate('2026-10-12T18:00:00Z'),tmp_path)


def test_missing_cutoff_is_honest_empty_and_cannot_replace_known_data(tmp_path):
    empty=candidate('2026-10-12T17:00:00Z');empty['data_cutoff']=None
    first=write_report(empty,tmp_path);assert_aliases(first)
    real=write_report(candidate('2026-10-12T18:00:00Z'),tmp_path)
    empty['generated_at']='2026-10-12T19:00:00Z'
    assert write_report(empty,tmp_path)['aliases_published']==[];assert_aliases(real)


def test_normal_publication_does_not_claim_crash_recovery(tmp_path):
    assert write_report(candidate('2026-10-12T18:00:00Z'),tmp_path)['publication_recovered'] is False


@pytest.mark.parametrize('changes',[{'start_day':'../../outside'}, {'end_day':'2026-02-30'}, {'period':'../../outside'}, {'mode':'unknown'}, {'end_day':'2026-10-01'}])
def test_invalid_legacy_metadata_rejected_before_archive_read(tmp_path,monkeypatch,changes):
    import json
    from finplan_platform.reporting.transport import decode_transport,encode_transport
    result=write_report(candidate('2026-10-12T18:00:00Z'),tmp_path)
    target=Path(result['directory']);data=decode_transport(json.loads((target/'latest.json').read_bytes()));data.update(changes)
    (target/'latest.json').write_text(json.dumps(encode_transport(data)))
    (target/'.report-heads/latest.json').unlink()
    original=publication._read;outside=[]
    def guarded(path,*args,**kwargs):
        if 'archive' in path.parts:
            assert '..' not in path.parts
            if not str(path.resolve()).startswith(str(target.resolve())+'/'):outside.append(str(path))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(publication,'_read',guarded)
    with pytest.raises(EvidenceError,match='Invalid legacy report'):write_report(candidate('2026-10-12T19:00:00Z'),tmp_path)
    assert not outside
