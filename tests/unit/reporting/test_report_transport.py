"""Lossless transport limits and cross-run deduplication (controlled fixtures only)."""
import copy
import json
import base64
import gzip
import pytest
from finplan_platform.reporting.transport import (
    pack_bytes, unpack_bytes, compact_report, encode_transport, decode_transport,
    canonical, sha, MAX_TRANSPORT_BYTES, unpack_json_bytes,
)
from test_modeling_reports import Reader, report


def test_original_noncanonical_bytes_order_repetitions_and_chunk_tampering():
    raw = b'{ "actual" : "' + b'0123456789abcdef'*10000 + b'", "ordered":[3,1,3] }\n'
    pool={}; captured=pack_bytes(raw,pool)
    assert len(captured['raw_chunks'])>1 and len(set(captured['raw_chunks']))<len(captured['raw_chunks'])
    assert unpack_bytes(captured,pool)==raw
    wrong=copy.deepcopy(captured);wrong['raw_chunks']=list(reversed(wrong['raw_chunks']))
    with pytest.raises(ValueError,match='Complete original'):unpack_bytes(wrong,pool)
    damaged=copy.deepcopy(pool);damaged[captured['raw_chunks'][0]]['data']=base64.b64encode(b'corrupt').decode()
    with pytest.raises(ValueError,match='Compressed evidence'):unpack_bytes(captured,damaged)


def test_changed_ids_and_time_reuse_stable_original_subfields_without_regeneration():
    common=[{'symbol':'TEST','timestamp':f'2026-10-12T{n%24:02}:00:00Z','value':n/19} for n in range(2000)]
    first=json.dumps({'run_id':'A','decision_time':'2026-10-12T14:00:00Z','history':common},indent=1).encode()
    second=json.dumps({'run_id':'B_longer_header','decision_time':'2026-10-13T15:00:00Z','history':common},indent=1).encode()
    pool={};a=pack_bytes(first,pool);before=sum(len(v['data']) for v in pool.values());b=pack_bytes(second,pool)
    assert sha(first)!=sha(second) and len(set(a['raw_chunks'])&set(b['raw_chunks']))>len(set(a['raw_chunks']))*.8
    assert sum(len(v['data']) for v in pool.values())-before<before*.15
    assert unpack_bytes(a,pool)==first and unpack_bytes(b,pool)==second


def test_codec_reversible_columns_strings_literals_and_lossless_source_documents():
    source=report();source['runs'][0]['output']={'large_actual_context':'evidence'*1000,'$s':2}
    source['runs'][0]['metadata']={'trigger':'systemd_scheduled','audit':'original'}
    source['runs'][1]['metadata']={'trigger':'manual_validation'}
    compact=compact_report(source);encoded=encode_transport(compact);decoded=decode_transport(encoded)
    # The byte-page storage representation is lazy; normalized values and every
    # original index/token byte remain identical to the legacy representation.
    pools={'artifact_chunks','artifact_lexemes','artifact_pages'}
    assert {k:v for k,v in decoded.items() if k not in pools}=={k:v for k,v in compact.items() if k not in pools}
    assert set(decoded['artifact_chunks'])==set(compact['artifact_chunks'])
    for cid,row in compact['artifact_chunks'].items():
        original=gzip.decompress(base64.b64decode(row['data']))
        assert unpack_bytes({'checksum':cid,'size_bytes':row['size_bytes'],'raw_chunks':[cid]},decoded['artifact_chunks'])==original
    from finplan_platform.reporting.transport import _decode_lexeme
    assert [_decode_lexeme(v,decoded['artifact_lexemes'].page_store)for v in decoded['artifact_lexemes']]==[_decode_lexeme(v)for v in compact['artifact_lexemes']]
    for original,row in zip(source['runs'],decoded['runs']):
        assert json.loads(unpack_json_bytes(row['source_evidence_ref'],decoded['artifact_chunks'],decoded['artifact_lexemes']))==original
    for original,row in zip(source['observations'],decoded['observations']):
        assert json.loads(unpack_json_bytes(row['source_evidence_ref'],decoded['artifact_chunks'],decoded['artifact_lexemes']))==original
    assert decoded['runs'][0]['trigger']=='systemd_scheduled'
    assert decoded['runs'][1]['trigger']=='manual_validation'
    assert 'output' not in decoded['runs'][0] and 'metadata' not in decoded['runs'][0]
    for rid,capture in decoded['inputs'].items():
        assert unpack_json_bytes(capture,decoded['artifact_chunks'],decoded['artifact_lexemes'])==base64.b64decode(source['inputs'][rid]['raw_base64'])
        assert json.loads(unpack_json_bytes(capture['source_metadata_ref'],decoded['artifact_chunks'],decoded['artifact_lexemes']))==source['inputs'][rid]['metadata']
    # Exact supplied numerical data lives in selected original bytes; the inline
    # capability summary cannot masquerade as generic replacement observations.
    assert decoded['inputs']['run_rule_0']['payload']['numeric']['prepared']['observation'] is None
    assert decoded['inputs']['run_rule_0']['preview_is_complete'] is False


def test_input_and_total_transport_limits_are_explicit_and_never_truncate(monkeypatch):
    import finplan_platform.reporting.transport as t
    monkeypatch.setattr(t,'MAX_ARTIFACT_BYTES',32)
    with pytest.raises(ValueError,match='not truncated'):pack_bytes(b'x'*33,{})
    monkeypatch.setattr(t,'MAX_ARTIFACT_BYTES',32*1024*1024)
    monkeypatch.setattr(t,'MAX_TRANSPORT_BYTES',100)
    with pytest.raises(ValueError,match='no evidence was silently truncated'):encode_transport(report())


def test_gzip_expansion_cannot_allocate_beyond_declared_original_bound():
    compressed=gzip.compress(b'X'*1000000); cid=sha(b'X')
    capture={'size_bytes':1,'checksum':cid,'raw_chunks':[cid]}
    pool={cid:{'size_bytes':1,'checksum':cid,'compressed_checksum':sha(compressed),'data':base64.b64encode(compressed).decode()}}
    with pytest.raises(ValueError,match='expansion'):unpack_bytes(capture,pool)


def test_report_revision_exports_compacted_self_contained_decodeable_json(tmp_path):
    from finplan_platform.reporting.reports import write_report
    source=report();written=write_report(source,tmp_path)
    encoded=json.loads((tmp_path/'replay'/'latest.json').read_bytes());decoded=decode_transport(encoded)
    assert encoded['transport_codec']=='modeling-report-gzip/1'
    assert encoded['limits']['max_transport_bytes']==MAX_TRANSPORT_BYTES
    assert len(decoded['runs'])==len(source['runs'])
    assert written['manifest']['source_record_ids']==source['source_record_ids']
    assert decoded['leaderboard']==source['leaderboard']
    assert decoded['forecasts']==source['forecasts']


def test_original_unpacked_and_earlier_columnar_exports_remain_readable():
    source=report()
    assert decode_transport(source) is source
    data=dict(source)
    for field in ('runs','observations'):
        rows=data.pop(field);keys=sorted({k for row in rows for k in row});data[field+'_columns']=keys;data[field+'_values']=[[row.get(k) for k in keys] for row in rows]
    data['input_values']=list(data.pop('inputs').items());pool=data.pop('artifact_chunks')
    earlier={'transport_codec':'modeling-report-columnar/1','strings':[],'data':data,'artifact_chunks':pool}
    decoded=decode_transport(earlier)
    assert [r['run_id'] for r in decoded['runs']]==[r['run_id'] for r in source['runs']]
    assert decoded['inputs']==source['inputs']


def test_transient_staging_and_cached_lexeme_varints_preserve_exact_original_format():
    from finplan_platform.reporting import transport as t
    raw=(b'{ "number":1.0000,"negative":-0.0,"scientific":1e-07,"unicode":"\\u0041","array":['+b','.join([b'0.00100']*12000)+b'] }')
    staging={};capture=t.pack_transient_bytes(raw,staging)
    assert t.unpack_bytes(capture,staging)==raw
    assert all(c['size_bytes']<=65536 for c in staging.values())
    optimized_pool={};optimized_lexemes=t.Lexemes()
    optimized=t.pack_json_bytes(raw,optimized_pool,optimized_lexemes)
    # Reference the old uncached lexical loop. This asserts token/chunk bytes and
    # dictionary order, beyond checking only equivalent parsed financial values.
    reference_pool={};reference_lexemes=t.Lexemes();refs=[];indices=bytearray();count=0
    for match in t._JSON_TOKEN.finditer(raw):
        indices.extend(t._varint(reference_lexemes.intern(match.group())));count+=1
        if count%1024==0:refs.append(t._pack_index_chunk(bytes(indices),reference_pool));indices.clear()
    if indices:refs.append(t._pack_index_chunk(bytes(indices),reference_pool))
    assert optimized['lexeme_chunks']==refs
    assert optimized_pool==reference_pool and optimized_lexemes.values==reference_lexemes.values
    before=len(optimized_lexemes.encoded_indices)
    assert t.pack_json_bytes(raw,optimized_pool,optimized_lexemes)==optimized
    assert len(optimized_lexemes.encoded_indices)==before==len(optimized_lexemes.indices)
    assert t.unpack_json_bytes(optimized,optimized_pool,optimized_lexemes.values)==raw


def test_transient_duplicate_chunk_boundaries_reuse_and_complete_original_hash():
    from finplan_platform.reporting import transport as t
    block=b'x'*65536;raw=block*3+b'end'
    pool={};capture=t.pack_transient_bytes(raw,pool)
    assert len(pool)==2 and capture['raw_chunks'][:3]==[capture['raw_chunks'][0]]*3
    assert t.unpack_bytes(capture,pool)==raw
    # Same-size substitution of a repeated chunk must fail the full originalSHA.
    wrong=t.pack_transient_bytes(b'y'*65536,pool)['raw_chunks'][0]
    altered={**capture,'raw_chunks':[wrong,*capture['raw_chunks'][1:]]}
    import pytest
    with pytest.raises(ValueError,match='Complete original'):
        t.unpack_bytes(altered,pool)


def test_cached_lexical_fast_path_rejects_unrecognized_bytes_and_bounds_large_stream():
    from finplan_platform.reporting import transport as t
    import pytest
    for raw in (b'{"value":1}@',b'{"value":@1}',b'@{"value":1}'):
        with pytest.raises(ValueError,match='unrecognized'):
            t.pack_json_bytes(raw,{},t.Lexemes())
    raw=b'{"large":"'+b'x'*(1024*1024+10)+b'","value":1.0000}'
    pool={};lexemes=t.Lexemes();captured=t.pack_json_bytes(raw,pool,lexemes)
    assert t.unpack_json_bytes(captured,pool,lexemes.values)==raw


def test_transient_fast_gzip_keeps_repeated_chunks_and_corruption_checks():
    from finplan_platform.reporting.transport import pack_transient_bytes
    raw = b'{ "lexical":[1.0000,-0.0,1e-07], "text":"' + b'0123456789abcdef'*22000 + b'" }\n'
    pool = {}; capture = pack_transient_bytes(raw, pool)
    assert len(capture['raw_chunks']) > len(set(capture['raw_chunks']))
    assert unpack_bytes(capture, pool) == raw
    damaged = copy.deepcopy(pool)
    damaged[capture['raw_chunks'][1]]['data'] = base64.b64encode(b'corrupt').decode()
    with pytest.raises(ValueError, match='Compressed evidence'):
        unpack_bytes(capture, damaged)


def test_temporary_gzip_levels_produce_identical_final_original_byte_transport():
    from finplan_platform.reporting.transport import pack_transient_bytes
    from finplan_platform.reporting.reports import captured_input_bytes
    source = report();pool = {}
    for run_id, capture in source['inputs'].items():
        original = base64.b64decode(capture['raw_base64'])
        raw = original.rstrip()[:-1] + b', "lexical":[1.0000,-0.0,1e-07], "text":' + json.dumps('original escaped \\ quote " unicode é '*3000,ensure_ascii=False).encode() + b'}'
        metadata = {**capture['metadata'], 'checksum':sha(raw)}
        source['inputs'][run_id] = {**capture, **pack_transient_bytes(raw,pool), 'metadata':metadata, 'payload':json.loads(raw)}
        source['inputs'][run_id].pop('raw_base64')
    source['artifact_chunks'] = pool
    legacy = copy.deepcopy(source)
    for chunk in legacy['artifact_chunks'].values():
        original = gzip.decompress(base64.b64decode(chunk['data']))
        compressed = gzip.compress(original,compresslevel=6,mtime=0)
        chunk.update(data=base64.b64encode(compressed).decode(),compressed_checksum=sha(compressed))
    encoded = encode_transport(source)
    assert canonical(encoded) == canonical(encode_transport(legacy))
    decoded = decode_transport(encoded)
    for run_id,capture in decoded['inputs'].items():
        assert captured_input_bytes(decoded,capture) == captured_input_bytes(source,source['inputs'][run_id])
