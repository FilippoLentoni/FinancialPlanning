"""Parallel report reads preserve immutable bytes, statuses and scoped ordering."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from threading import Lock
import time
from types import SimpleNamespace

import pytest

from finplan_platform.core.errors import PlatformError
from finplan_platform.core.modeling_evidence import ModelingEvidence
from finplan_platform.reporting.cache import ArtifactCache
from finplan_platform.reporting.reader import MAX_ARTIFACT_BYTES, ReportReader, prefetched_inputs
from finplan_platform.reporting.reports import _input_for_run, checksum


def ref(raw, name="artifact"):
    return {"artifact_key": "modeling-evidence/v1/test/"+name+".json", "checksum": checksum(raw), "size_bytes": len(raw), "bucket_role": "reports"}


class Backend(ModelingEvidence):
    def __init__(self, count=40, *, fail=None):
        self.records = {}
        self.raws = {}
        self.calls = Counter()
        self.active = self.peak = 0
        self.lock = Lock()
        self.fail = fail
        for i in range(count):
            rid = f"r{i:03d}"
            raw = json.dumps({"status": "succeeded", "number": i}).encode()
            output = ref(raw, rid)
            self.raws[output["artifact_key"]] = raw
            self.records[rid] = SimpleNamespace(id=rid, attrs={}, doc={"run_id": rid, "input_id": "i"+rid, "output_ref": output})
        self.repo = SimpleNamespace(require=self.require, query_index=self.query)

    def require(self, table, rid):
        with self.lock:
            self.calls[("metadata", table, rid)] += 1
        return self.records[rid]

    def query(self, table, index, partition, *, newest_first, limit, page_token):
        assert index == "day-index" and newest_first is False
        start = int(page_token or 0)
        rows = list(self.records.values())[start:start+limit]
        return rows, str(start+limit) if start+limit < len(self.records) else None

    def read_artifact_bytes(self, artifact):
        key = artifact["artifact_key"]
        with self.lock:
            self.calls[("artifact", key)] += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            # Different completions must not change row order.
            time.sleep(.001*(1+int(hashlib.sha256(key.encode()).hexdigest()[0],16)%4))
            if self.fail and self.fail in key:
                raise PlatformError.not_found("controlled missing artifact")
            return self.raws[key]
        finally:
            with self.lock:
                self.active -= 1


def test_parallel_day_expansion_bounded_ordered_and_failure_rows_retained():
    backend = Backend(41, fail="r017")
    reader = ReportReader(backend, workers=8)
    try:
        rows, token = reader.query_day("experiment", "2026-10-18", page_size=23)
        following, final = reader.query_day("experiment", "2026-10-18", page_size=23, next_token=token)
        assert [r["run_id"] for r in rows+following] == [f"r{i:03d}" for i in range(41)]
        assert final is None and 2 <= backend.peak <= 8
        failed = next(r for r in rows if r["run_id"] == "r017")
        assert failed["outcome"] is None and failed["evidence_error"]
        assert len(rows+following) == 41
        assert all(backend.calls[("metadata","research_run",f"r{i:03d}")] == 1 for i in range(41))
        with pytest.raises(PlatformError):
            reader.query_day("other", "2026-10-18", next_token=token)
        with pytest.raises(PlatformError):
            reader.query_day("experiment", "2026-10-18", kind="observations", next_token=token)
    finally:
        reader.close()


def test_original_input_read_once_lexical_bytes_not_regenerated():
    backend = Backend(0)
    raw = b'{ "approach_id":"cash", "input_id":"i", "run_id":"r", "decision_time":"2026-10-18T01:00:00Z", "numeric":{"prepared":{"x":1.0000,"y":-0.0}}, "news":{"consumed":false} }'
    doc = {**ref(raw), "input_id":"i", "approach_id":"cash", "kind":"model_input", "slot":"2026-10-18T01:00:00Z"}
    backend.records["i"] = SimpleNamespace(doc=doc)
    backend.raws[doc["artifact_key"]] = raw
    run={"run_id":"r","input_id":"i","approach_id":"cash","scheduled_slot":doc["slot"],"decision_time":doc["slot"]}
    reader=ReportReader(backend)
    try:
        captures=list(prefetched_inputs(reader,[run]))
        captured=_input_for_run(reader,run,captured=captures[0][1])
        import base64
        assert base64.b64decode(captured["raw_base64"]) == raw
        assert backend.calls[("metadata","research_input","i")] == 1
        assert backend.calls[("artifact",doc["artifact_key"])] == 1
        assert captured["checksum"] == checksum(raw)
        bad={**run,"approach_id":"other"}
        with pytest.raises(ValueError,match="approach_id"):
            _input_for_run(reader,bad,captured=captures[0][1])
    finally:reader.close()


def test_singleflight_and_persistent_cache_reverify_bytes_and_fresh_metadata(tmp_path):
    backend=Backend(1); original=backend.records['r000'].doc['output_ref']
    reader=ReportReader(backend,cache_dir=tmp_path/'cache')
    try:
        assert list(reader.ordered_map(reader.read_artifact_bytes,[original]*40)) == [backend.raws[original['artifact_key']]]*40
        assert backend.calls[("artifact",original['artifact_key'])] == 1
    finally:reader.close()
    reader=ReportReader(backend,cache_dir=tmp_path/'cache')
    try:
        first=reader.read_run('r000');second=reader.read_run('r000')
        assert first==second and backend.calls[("metadata","research_run","r000")] == 2
        assert backend.calls[("artifact",original['artifact_key'])] == 1
        with pytest.raises(PlatformError):reader.read_artifact_bytes({**original,"artifact_key":"unapproved/path"})
        with pytest.raises(ValueError):reader.read_artifact_bytes({**original,"size_bytes":MAX_ARTIFACT_BYTES+1})
        with pytest.raises(ValueError):reader.read_artifact_bytes({**original,"size_bytes":1})
    finally:reader.close()
    cached=tmp_path/'cache'/(original['checksum'][7:]+'.bin')
    cached.write_bytes(b'CORRUPT')
    reader=ReportReader(backend,cache_dir=tmp_path/'cache')
    try:
        assert reader.read_artifact_bytes(original)==backend.raws[original['artifact_key']]
        assert backend.calls[("artifact",original['artifact_key'])] == 2
    finally:reader.close()
    assert (tmp_path/'cache').stat().st_mode&0o777==0o700
    assert all(p.stat().st_mode&0o077==0 for p in (tmp_path/'cache').iterdir())


def test_cache_byte_entry_eviction_is_process_coordinated_and_preserves_unrelated_files(tmp_path):
    directory=tmp_path/'cache'
    a=ArtifactCache(directory,max_bytes=12,max_entries=2)
    b=ArtifactCache(directory,max_bytes=12,max_entries=2)
    unrelated=directory/'keep.txt';unrelated.write_text('keep')
    try:
        a.put(checksum(b'11111'),b'11111');b.put(checksum(b'22222'),b'22222')
        assert a.get(checksum(b'11111'),max_size=32)==b'11111'
        b.put(checksum(b'33333'),b'33333')
        assert b.get(checksum(b'22222'),max_size=32) is None
        assert sum(p.stat().st_size for p in directory.glob('*.bin'))<=12
        assert len(list(directory.glob('*.bin')))<=2
        assert unrelated.read_text()=='keep'
        with pytest.raises(ValueError):a.put(checksum(b'wrong'),b'11111')
    finally:a.close();b.close()


def test_real_backend_reader_matches_strong_decoded_run_and_scoped_queries(repo_any,artifacts):
    backend=ModelingEvidence(repo_any,artifacts)
    at='2026-10-18T01:00:00Z'
    book=backend.ensure_portfolio('report-io-test','cash',1000,['SPY'],at)
    captured=backend.capture_input('report-io-test','cash',at,{'decision_time':at,'availability_cutoff':at,'news':{'consumed':False}},None,at)
    run=backend.start_run('report-io-test','cash',at,captured['input_id'],book['portfolio_id'],at,{'family':'control'})
    backend.finish_run(run['run_id'],{'status':'hold'},at)
    expected=backend.read_run(run['run_id'])
    reader=ReportReader(backend)
    try:
        if reader.backend is not backend:
            assert reader.backend.repo.client.meta.config.max_pool_connections==32
            assert reader.backend.store._s3.meta.config.max_pool_connections==32
            assert reader.backend.repo.names==backend.repo.names
            assert reader.backend.store._s3.meta.endpoint_url==backend.store._s3.meta.endpoint_url
            assert reader.backend.store._s3.meta.config.signature_version==backend.store._s3.meta.config.signature_version
        assert reader.read_run(run['run_id'])==expected
        assert reader.query_day('report-io-test','2026-10-17')==backend.query_day('report-io-test','2026-10-17')
        actual=reader.read_input(captured['input_id'])
        assert actual['raw_bytes']==backend.read_input_bytes(captured['input_id'])
        assert actual['metadata']==backend.read_input(captured['input_id'])['metadata']
    finally:reader.close()


def _cache_process(directory, prefix):
    cache=ArtifactCache(Path(directory),max_bytes=128,max_entries=7)
    try:
        for number in range(30):
            raw=(prefix+str(number)).encode()*3
            cache.put(checksum(raw),raw)
            recovered=cache.get(checksum(raw),max_size=128)
            assert recovered in (None,raw)  # The other process may evict it.
    finally:cache.close()


def test_cache_two_processes_keep_byte_count_bounds_after_restart(tmp_path):
    import multiprocessing
    directory=tmp_path/'process-cache'
    context=multiprocessing.get_context('fork')
    children=[context.Process(target=_cache_process,args=(str(directory),prefix)) for prefix in ('one','two')]
    for child in children:child.start()
    for child in children:
        child.join(timeout=20)
        assert child.exitcode==0
    cache=ArtifactCache(directory,max_bytes=128,max_entries=7)
    try:
        files=list(directory.glob('*.bin'))
        assert len(files)<=7 and sum(p.stat().st_size for p in files)<=128
        assert not list(directory.glob('.tmp-*'))
        for p in files:assert checksum(p.read_bytes())=='sha256:'+p.stem
    finally:cache.close()


def test_input_prefetch_failure_retains_order_and_does_not_pack_in_worker():
    backend=Backend(0)
    class Reader(ReportReader):
        def read_input(self,iid):
            with backend.lock:
                backend.active+=1;backend.peak=max(backend.peak,backend.active)
            try:
                time.sleep(.002)
                if iid=='bad':raise PlatformError.not_found('controlled missing original')
                return {'raw_bytes':iid.encode(),'payload':{'input_id':iid},'metadata':{'input_id':iid}}
            finally:
                with backend.lock:backend.active-=1
    reader=Reader(backend,workers=8)
    runs=[{'run_id':str(i),'input_id':'bad' if i==9 else 'i'+str(i)}for i in range(50)]
    try:
        result=list(prefetched_inputs(reader,runs))
        assert [pair[0]['run_id']for pair in result]==[str(i)for i in range(50)]
        assert isinstance(result[9][1],PlatformError)
        assert all(pair[1]['raw_bytes']==pair[0]['input_id'].encode()for i,pair in enumerate(result)if i!=9)
        assert 2<=backend.peak<=8
    finally:reader.close()


def test_cache_failure_falls_back_to_verified_original_and_preserves_unrelated_temp(tmp_path,monkeypatch):
    directory=tmp_path/'cache'
    initial=ArtifactCache(directory);initial.close()
    unrelated=directory/'.tmp-unrelated';unrelated.write_text('keep')
    backend=Backend(1);original=backend.records['r000'].doc['output_ref']
    reader=ReportReader(backend,cache_dir=directory)
    try:
        def no_space(*args,**kwargs):raise OSError('controlled disk full')
        monkeypatch.setattr(reader._disk,'put',no_space)
        assert reader.read_artifact_bytes(original)==backend.raws[original['artifact_key']]
        assert reader.io_status()['cache_failures']==['OSError']
        assert not reader.io_status()['disk_cache_enabled']
    finally:reader.close()
    assert unrelated.read_text()=='keep'
    (directory/'cache.sqlite').write_bytes(b'controlled invalid sqlite')
    for suffix in ('-wal','-shm'):(directory/('cache.sqlite'+suffix)).unlink(missing_ok=True)
    reader=ReportReader(backend,cache_dir=directory)
    try:
        assert reader.read_artifact_bytes(original)==backend.raws[original['artifact_key']]
        assert reader.cache_failures and not reader.io_status()['disk_cache_enabled']
    finally:reader.close()


def test_cache_write_failure_is_operational_status_not_a_model_evidence_failure(repo,artifacts,tmp_path,monkeypatch):
    from finplan_platform.reporting.reports import aggregate,captured_input_bytes
    backend=ModelingEvidence(repo,artifacts);at='2026-10-18T01:00:00Z'
    book=backend.ensure_portfolio('report-cache-fallback','cash',1000,['SPY'],at)
    input_ref=backend.capture_input('report-cache-fallback','cash',at,{'decision_time':at,'availability_cutoff':at,'news':{'consumed':False},'numeric':{'prepared':{'rule':'hold cash'}}},None,at)
    run=backend.start_run('report-cache-fallback','cash',at,input_ref['input_id'],book['portfolio_id'],at,{'family':'control'})
    backend.finish_run(run['run_id'],{'status':'hold'},at)
    monkeypatch.setenv('FINPLAN_REPORT_CACHE_DIR',str(tmp_path/'cache'))
    def full(*args,**kwargs):raise OSError('controlled cache disk full')
    monkeypatch.setattr(ArtifactCache,'put',full)
    report=aggregate(backend,experiment_id='report-cache-fallback',registry=[{'approach_id':'cash','family':'control','portfolio_id':book['portfolio_id']}],start_day='2026-10-17',generated_at=at)
    assert report['evidence_failures']==[]
    assert report['runs'][0]['status']=='hold'
    assert report['report_io']['cache_failures']==['OSError']
    assert captured_input_bytes(report,report['inputs'][run['run_id']])==backend.read_input_bytes(input_ref['input_id'])


def test_cache_beneficial_compression_bounds_original_expansion_and_full_sha(tmp_path):
    raw=b'{"large":"'+b'original '*20000+b'","number":1.0000}'
    cache=ArtifactCache(tmp_path/'cache',max_bytes=4096,max_entries=2)
    try:
        cache.put(checksum(raw),raw)
        cached=tmp_path/'cache'/(checksum(raw)[7:]+'.bin')
        assert cached.stat().st_size<4096
        assert cache.get(checksum(raw),max_size=len(raw))==raw
        content=cached.read_bytes()
        assert content.startswith(b'\x00FMCA1')
        cached.write_bytes(content[:6]+(len(raw)+1).to_bytes(8,'big')+content[14:])
        assert cache.get(checksum(raw),max_size=len(raw)) is None
        cache.put(checksum(raw),raw)
        altered=bytearray(cached.read_bytes());altered[-1]^=1;cached.write_bytes(altered)
        assert cache.get(checksum(raw),max_size=len(raw)) is None
    finally:cache.close()


def test_report_owned_client_pool_preserves_supplied_scope_not_default_caller(artifacts):
    import boto3
    from finplan_platform.core.repository import MetadataRepository
    supplied=boto3.client('dynamodb',region_name='us-east-2',aws_access_key_id='scoped-test-key',aws_secret_access_key='scoped-test-secret',aws_session_token='scoped-test-token')
    backend=ModelingEvidence(MetadataRepository(supplied,'beta'),artifacts)
    reader=ReportReader(backend)
    try:
        assert reader.backend is not backend
        assert reader.backend.repo.client._request_signer._credentials is supplied._request_signer._credentials
        assert reader.backend.store._s3._request_signer._credentials is artifacts._s3._request_signer._credentials
        assert reader.backend.repo.client.meta.config.max_pool_connections==32
        assert supplied.meta.config.max_pool_connections!=32
    finally:reader.close();supplied.close()


def test_singleflight_waiter_checks_its_own_conflicting_declared_size():
    from threading import Event
    backend=Backend(1);reference=backend.records['r000'].doc['output_ref']
    entered=Event();release=Event();original=backend.read_artifact_bytes
    def delayed(ref):
        entered.set();assert release.wait(timeout=5)
        return original(ref)
    backend.read_artifact_bytes=delayed
    reader=ReportReader(backend)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(reader.read_artifact_bytes,reference)
            assert entered.wait(timeout=5)
            waiter=pool.submit(reader.read_artifact_bytes,{**reference,'size_bytes':1})
            time.sleep(.01);release.set()
            assert first.result()==backend.raws[reference['artifact_key']]
            with pytest.raises(ValueError,match='size differs'):waiter.result()
        assert backend.calls[('artifact',reference['artifact_key'])]==1
    finally:release.set();reader.close()
