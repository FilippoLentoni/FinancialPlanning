"""Controlled byte-page integrity, compatibility and distinct builder limits."""
import base64
import copy
import gzip
import json
import pytest
from finplan_platform.reporting import transport as t
from finplan_platform.reporting import pages as p
from test_modeling_reports import report
from finplan_platform.reporting.descriptor_table import decode as descriptors, expand_small_offsets

def original_rows(encoded):
    return descriptors(encoded['artifact_chunk_table']) if 'artifact_chunk_table' in encoded else encoded['artifact_chunk_values']

def original_tokens(encoded):
    return expand_small_offsets(encoded['artifact_lexemes'], encoded.get('artifact_lexeme_offsets'))


def columnar(raw=b'{ "n":-0.00,"e":1e-07,"s":"\\u0041" }\n'):
    pool={}; lex=t.Lexemes(page_transport=True)
    capture=t.pack_json_bytes(raw,pool,lex)
    result={'transport_codec':t.CODEC,'strings':[], 'data':{},
            'artifact_chunk_values':[[cid,v['size_bytes'],v['data']]for cid,v in pool.items()],
            'artifact_lexemes':lex.values,'limits':{}}
    return raw,capture,result


def test_original_noncanonical_byte_stream_and_original_index_shas_survive_pages():
    raw,capture,old=columnar()
    encoded=p.page_columnar(old); store=p.PageStore(encoded['index_pages'],encoded['lexeme_pages'])
    chunks=p.PageChunks({cid:{'encoding':'byte-page-range/1','size_bytes':size,'offset':offset,'checksum':cid}for cid,size,offset in original_rows(encoded)},store)
    lex=p.PageLexemes(original_tokens(encoded),store)
    assert set(chunks)=={v[0]for v in old['artifact_chunk_values']}
    assert t.unpack_json_bytes(capture,chunks,lex)==raw


def test_unicode_token_crosses_page_without_decoding_partial_utf8_and_lru_is_bounded():
    raw=b'x'*(p.PAGE_BYTES-2)+'\U0001f680'.encode()+b'end'
    writer=p.PageWriter();writer.append(raw); pages=writer.finish()
    store=p.PageStore([],pages)
    assert store.read('lexeme',p.PAGE_BYTES-2,4).decode()=='\U0001f680'
    many=p.packed_pages(b'x'*(p.PAGE_BYTES*10))
    store=p.PageStore(many,[])
    for n in range(10):assert store.page('index',n)==b'x'*p.PAGE_BYTES
    assert store.cache_bytes==p.PAGE_CACHE_BYTES and len(store.cache)==8
    with pytest.raises(ValueError):store.page('index',True)


def test_page_corruption_and_invalid_ranges_are_rejected():
    pages=p.packed_pages(b'actual supplied bytes')
    bad=copy.deepcopy(pages);bad[0]['checksum']=p.sha(b'other')
    with pytest.raises(ValueError,match='checksum'):p.PageStore(bad,[]).read('index',0,1)
    store=p.PageStore(pages,[])
    for offset,size in ((-1,1),(0,True),(1000,1),(0,p.ARTIFACT_BYTES+1)):
        with pytest.raises(ValueError):store.read('index',offset,size)


@pytest.mark.parametrize('size',[True,-1,p.ARTIFACT_BYTES+1])
def test_invalid_original_index_size_fails_before_gzip_expansion(size,monkeypatch):
    _,_,old=columnar();old['artifact_chunk_values'][0][1]=size
    original=p.zlib.decompressobj
    calls=[]
    monkeypatch.setattr(p.zlib,'decompressobj',lambda *a:(calls.append(a),original(*a))[1])
    with pytest.raises(ValueError):p.page_columnar(old)
    assert calls==[]


def test_direct_appended_dictionary_cannot_bypass_incremental_builder_budget(monkeypatch):
    _,_,old=columnar(); old['artifact_lexemes'].append(base64.b64encode(b'x'*2000).decode())
    monkeypatch.setattr(p,'MAX_BUILDER_MEMORY_BYTES',1000)
    with pytest.raises(ValueError,match='builder-memory'):p.page_columnar(old)


def test_legacy_wire_guard_stays_while_paged_builder_has_separate_limit(monkeypatch):
    monkeypatch.setattr(t,'MAX_TRANSPORT_BYTES',10)
    with pytest.raises(ValueError,match='150MiB'):t.Lexemes().intern(b'long-original-token')
    paged=t.Lexemes(page_transport=True);paged.intern(b'long-original-token')
    monkeypatch.setattr(p,'MAX_BUILDER_MEMORY_BYTES',10)
    with pytest.raises(ValueError,match='builder-memory'):t.Lexemes(page_transport=True).intern(b'long-original-token')


def test_production_guards_final_representation_and_reencodes_original_bytes(monkeypatch):
    compact=t.compact_report(report()); legacy=t._encode_columnar(compact,measure=False,page_templates=True)
    paged=p.page_columnar(legacy)
    assert len(t.canonical(paged))<len(t.canonical(legacy))
    monkeypatch.setattr(t,'MAX_DECODED_TRANSPORT_BYTES',len(t.canonical(paged))+1)
    with pytest.raises(ValueError,match='384MiB'):t._encode_columnar(compact)
    encoded=t.encode_transport(compact); restored=t.decode_transport(encoded)
    # The injected one-byte headroom proves the first final-format guard.
    # A re-render may choose a different normalized dictionary/order.
    monkeypatch.setattr(t,'MAX_DECODED_TRANSPORT_BYTES',384*1024*1024)
    again=t.decode_transport(t.encode_transport(restored))
    for rid,capture in restored['inputs'].items():
        assert t.unpack_json_bytes(capture,restored['artifact_chunks'],restored['artifact_lexemes'])==t.unpack_json_bytes(again['inputs'][rid],again['artifact_chunks'],again['artifact_lexemes'])


def test_mixed_paged_and_legacy_ancillary_pool_is_explicitly_rejected():
    restored=t.decode_transport(t.encode_transport(report()))
    restored.pop('ancillary_evidence_compacted')
    with pytest.raises(ValueError,match='mixed index encodings'):t.encode_transport(restored)


def test_string_resolution_orders_page_offsets_without_reordering_dictionary():
    tokens=[json.dumps('later').encode(),json.dumps('earlier').encode()]
    store=p.PageStore([],p.packed_pages(b''.join(tokens)))
    encoded={'strings':[{'$lex':1},'literal',{'$lex':0}], 'artifact_lexemes':[[0,len(tokens[0])],[len(tokens[0]),len(tokens[1])]]}
    assert p.restore_page_strings(encoded,store)==['earlier','literal','later']


def test_fresh_index_chunks_and_lexemes_share_one_incremental_budget(monkeypatch):
    monkeypatch.setattr(p,'MAX_BUILDER_MEMORY_BYTES',1500)
    pool=p.BuilderChunks();lex=t.Lexemes(page_transport=True,budget=pool.budget)
    lex.intern(b'actual supplied token')
    # Unique index chunks allocate memory even when the lexical dictionary is tiny.
    with pytest.raises(ValueError,match='builder-memory'):
        for n in range(100):t._pack_index_chunk(bytes([n]),pool)
    assert lex.builder_bytes<1500


def test_source_encoded_strings_are_referenced_not_charged_as_new_conversion_allocations(monkeypatch):
    _,_,old=columnar()
    # An existing long compressed token is validated and streamed, not retained
    # as an additional newly owned token dictionary during conversion.
    token=b'"'+b'x'*100000+b'"'
    old['artifact_lexemes'].append([len(token),base64.b64encode(gzip.compress(token)).decode()])
    monkeypatch.setattr(p,'MAX_BUILDER_MEMORY_BYTES',200000)
    assert p.page_columnar(old)['transport_codec']==p.PAGE_CODEC


def test_large_lexical_fragments_reuse_bytes_with_exact_changed_token_checksum():
    first=b'"'+b'common public headline context '*2000+b'original"'
    second=first[:-9]+b'updated"'
    pool={};lex=t.Lexemes(page_transport=True)
    a=t.pack_json_bytes(first,pool,lex);b=t.pack_json_bytes(second,pool,lex)
    old={'transport_codec':t.CODEC,'strings':[],'data':{},'limits':{},
         'artifact_chunk_values':[[cid,v['size_bytes'],v['data']]for cid,v in pool.items()],'artifact_lexemes':lex.values}
    paged=p.page_columnar(old);store=p.PageStore(paged['index_pages'],paged['lexeme_pages'])
    values=p.PageLexemes(original_tokens(paged),store)
    chunks=p.PageChunks({cid:{'encoding':'byte-page-range/1','checksum':cid,'size_bytes':size,'offset':offset}for cid,size,offset in original_rows(paged)},store)
    assert t.unpack_json_bytes(a,chunks,values)==first and t.unpack_json_bytes(b,chunks,values)==second
    large=[v for v in values if isinstance(v,dict)]
    assert len(large)==2 and large[0]['parts'][:-1]==large[1]['parts'][:-1]
    damaged=copy.deepcopy(large[0]);damaged['parts'][0]=damaged['parts'][-1]
    with pytest.raises(ValueError,match='size|checksum'):store.token(damaged)


def test_fragment_unicode_split_and_excessive_fragment_table_fail_closed():
    raw=b'x'*4095+'\U0001f680'.encode()+b'end';writer=p.PageWriter();parts=[]
    for at in range(0,len(raw),4096):part=raw[at:at+4096];parts.append([writer.append(part),len(part)])
    store=p.PageStore([],writer.finish());ref={'parts':parts,'size_bytes':len(raw),'checksum':p.sha(raw)}
    assert store.token(ref)==raw
    wrong={**ref,'parts':[[0,0]]*10000}
    with pytest.raises(ValueError,match='fragments'):store.token(wrong)


def test_paged_hash_keys_preserve_legacy_indices_and_reject_collisions():
    raw=b' {"values":[-0.00,1e-7,1,1,1],"context":"'+b'public headline '*1000+b'"}\n'
    a={};b={};legacy=t.Lexemes();paged=t.Lexemes(page_transport=True)
    x=t.pack_json_bytes(raw,a,legacy);y=t.pack_json_bytes(raw,b,paged)
    assert x==y and a==b and legacy.values==paged.values
    assert all(isinstance(k,int)for k in paged.encoded_indices)
    collision=t.Lexemes(page_transport=True);collision.token_key=lambda value:b'fixed-test-collision'
    collision.intern(b'first')
    with pytest.raises(ValueError,match='collision'):collision.intern(b'different')
    paged.hot_limit=512
    for n in range(100):paged.intern(str(n).encode())
    assert paged.hot_bytes<=512


def test_sparse_index_uses_one_plain_base_and_preserves_both_original_cids():
    from finplan_platform.reporting.index_delta import encode
    a=[(i*7919)%100000 for i in range(1024)];b=list(a);b[10]=123456;b[500]=222222
    raw_a=encode(a);raw_b=encode(b);pool={}
    cid_a=t._pack_index_chunk(raw_a,pool);cid_b=t._pack_index_chunk(raw_b,pool)
    old={'transport_codec':t.CODEC,'strings':[],'data':{},'limits':{},'artifact_lexemes':[],
         '_index_templates':{cid_a:'same-approach-layer-position',cid_b:'same-approach-layer-position'},
         'artifact_chunk_values':[[cid,v['size_bytes'],v['data']]for cid,v in pool.items()]}
    paged=p.page_columnar(old);rows=original_rows(paged);assert len(rows[0])==3 and len(rows[1])==6
    store=p.PageStore(paged['index_pages'],paged['lexeme_pages']);chunks=p.PageChunks({},store)
    for row in rows:
        cid,size,offset=row[:3];chunks[cid]={'encoding':'byte-page-range/1','checksum':cid,'size_bytes':size,'offset':offset}
        if len(row)==6:chunks[cid].update(encoding=row[3],encoded_size=row[4],base_chunk=rows[row[5]][0] if isinstance(row[5],int) else row[5])
    cap={'raw_chunks':[cid_b],'checksum':cid_b,'size_bytes':len(raw_b)}
    assert t.unpack_bytes(cap,chunks)==raw_b
    chunks[cid_a]['encoding']='sparse-index-delta/1'
    with pytest.raises(ValueError,match='plain|chains'):t.unpack_bytes(cap,chunks)


def test_sparse_patch_rejects_duplicate_out_of_range_noncanonical_and_truncated_values():
    from finplan_platform.reporting.index_delta import apply,encode
    base=encode([1,2,3])
    for patch in (encode([0,5,0,6]),encode([10,6]),b'\x80',b'\x81\x00\x01'):
        with pytest.raises(ValueError):apply(base,patch)
    with pytest.raises(ValueError):apply(b'\x81\x00',b'')


def test_browser_sparse_decoder_matches_exact_original_bytes_and_rejects_bad_positions():
    import shutil,subprocess
    from pathlib import Path
    from finplan_platform.reporting.index_delta import encode,patch
    node=shutil.which('node')
    if not node:pytest.skip('Node is required for the browser decoder cross-language check')
    a=[1,127,128,65535,0xffffffff];b=[1,127,555555,65535,0xffffffff]
    data={'base':list(encode(a)),'patch':list(patch(a,b)),'wrong':list(encode([99,1]))}
    script="const fs=require('node:fs'),vm=require('node:vm');vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));const d=JSON.parse(fs.readFileSync(0,'utf8'));const raw=__applyReportIndexDelta(Uint8Array.from(d.base),Uint8Array.from(d.patch));let rejected=false;try{__applyReportIndexDelta(Uint8Array.from(d.base),Uint8Array.from(d.wrong))}catch(error){rejected=true}process.stdout.write(JSON.stringify({raw:Array.from(raw),rejected}));"
    result=subprocess.run([node,'-e',script,str(Path(p.__file__).with_name('page_codec.js'))],input=json.dumps(data),text=True,capture_output=True,check=True)
    actual=json.loads(result.stdout)
    assert bytes(actual['raw'])==encode(b) and actual['rejected'] is True


def test_column_layout_preserves_missing_nulls_and_browser_rows():
    import shutil,subprocess
    from pathlib import Path
    compact=t.compact_report(report())
    ids=list(compact['inputs']); compact['inputs'][ids[0]]['explicit_null']=None
    encoded=t._encode_columnar(compact,measure=False,page_templates=True)
    assert encoded['data']['runs_layout']=='columns/1'
    restored=t.decode_transport(copy.deepcopy(encoded))
    assert restored['inputs'][ids[0]]['explicit_null'] is None
    assert all('explicit_null' not in value for rid,value in restored['inputs'].items() if rid!=ids[0])
    node=shutil.which('node')
    if not node:pytest.skip('Node is required for cross-language column layout check')
    script="const fs=require('node:fs'),vm=require('node:vm');vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8').split('const DATA=')[0]);process.stdout.write(JSON.stringify(decodeReportTransport(JSON.parse(fs.readFileSync(0,'utf8')))));"
    actual=json.loads(subprocess.run([node,'-e',script,str(Path(p.__file__).with_name('report.js'))],input=json.dumps(encoded),text=True,capture_output=True,check=True).stdout)
    assert actual['inputs']==restored['inputs'] and actual['runs']==restored['runs']
    bad=copy.deepcopy(encoded);bad['data']['runs_values'][0].append(None)
    with pytest.raises(ValueError,match='column lengths'):t.decode_transport(bad)
    bad=copy.deepcopy(encoded);bad['data']['input_presence'][0]=2**31
    with pytest.raises(ValueError,match='presence mask'):t.decode_transport(bad)


def test_local_canonical_varint_guard_matches_original_reencode_reference():
    import random
    from finplan_platform.reporting.index_delta import decode,encode
    rng=random.Random(73091)
    for _ in range(1000):
        values=[rng.randrange(2**32) for _ in range(rng.randrange(20))]
        raw=encode(values)
        assert list(decode(raw))==values
        if raw:
            # Add an overlong zero group after a legitimate terminal group.
            bad=raw[:-1]+bytes([raw[-1]|128,0])
            with pytest.raises(ValueError,match='Noncanonical|bound'):decode(bad)
    for bad in (b'\x80\x00',b'\xff\xff\xff\xff\x00',b'\x81\x80\x00'):
        with pytest.raises(ValueError,match='Noncanonical'):decode(bad)


def test_paged_fast_lookaside_preserves_every_boundary_and_warm_artifact():
    # More than1024 tokens and >1MiB both exercise complete and streamed batches.
    raw=t.canonical({'rows':[['é',-0.0,1e-7,True,None,'escaped\\"\\n']]*45000})
    assert len(raw)>1024*1024
    old_pool={};new_pool={};legacy=t.Lexemes();paged=t.Lexemes(page_transport=True)
    old=t.pack_json_bytes(raw,old_pool,legacy)
    new=t.pack_json_bytes(raw,new_pool,paged)
    assert old==new and old_pool==new_pool and legacy.values==paged.values
    calls=0;original=paged.intern
    def count(token):
        nonlocal calls
        calls+=1;return original(token)
    paged.intern=count
    again=t.pack_json_bytes(raw,new_pool,paged)
    assert again==old and calls==0
    assert t.unpack_json_bytes(again,new_pool,paged.values)==raw
    assert paged.hot_bytes<=paged.hot_limit<=8*1024*1024


def test_paged_fast_lookaside_eviction_still_checks_cold_collisions():
    paged=t.Lexemes(page_transport=True);paged.hot_limit=512
    paged.token_key=lambda token:b'controlled-collision'
    first=b'first';paged.intern(first)
    paged.hot.clear();paged.hot_bytes=0
    with pytest.raises(ValueError,match='collision'):
        t.pack_json_bytes(b'"different"',{},paged)
    # Invalid coverage must not be accepted by the batched lexical path.
    with pytest.raises(ValueError,match='unrecognized'):
        t.pack_json_bytes(b'{"x":1}invalid',{},t.Lexemes(page_transport=True))


def test_json_bulk_lexing_matches_original_escaped_and_invalid_intervals():
    import re,random
    original=re.compile(rb'"(?:\\.|[^"\\])*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null|\s+|[{}\[\],:]')
    corpus=[b'"\\\\\\\"\\u00e9\\t\\r\\n"',b'"unterminated',b'"bad\\\n"',b'1e+3 -0.00 1e-9 true false null',b'{"a":1}invalid']
    rng=random.Random(180)
    for _ in range(100):
        value=''.join(rng.choice('abcé💰\\"\n\t')for _ in range(300))
        corpus.append(t.canonical({'text':value,'number':rng.uniform(-100,100)}))
    for raw in corpus:
        assert t._JSON_TOKEN.findall(raw)==original.findall(raw)
    # Cold batches must assign indices in first appearance order despite repeats.
    raw=t.canonical({'new':['z','a','z','b','a']*1000})
    a={};b={};old=t.Lexemes();new=t.Lexemes(page_transport=True);new.hot_limit=512
    assert t.pack_json_bytes(raw,a,old)==t.pack_json_bytes(raw,b,new)
    assert a==b and old.values==new.values
