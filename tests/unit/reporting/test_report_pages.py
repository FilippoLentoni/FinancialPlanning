"""Controlled byte-page integrity, compatibility and distinct builder limits."""
import base64
import copy
import gzip
import json
import pytest
from finplan_platform.reporting import transport as t
from finplan_platform.reporting import pages as p
from test_modeling_reports import report


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
    chunks=p.PageChunks({cid:{'encoding':'byte-page-range/1','size_bytes':size,'offset':offset,'checksum':cid}for cid,size,offset in encoded['artifact_chunk_values']},store)
    lex=p.PageLexemes(encoded['artifact_lexemes'],store)
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
    compact=t.compact_report(report()); legacy=t._encode_columnar(compact,measure=False)
    paged=p.page_columnar(legacy)
    assert len(t.canonical(paged))<len(t.canonical(legacy))
    monkeypatch.setattr(t,'MAX_DECODED_TRANSPORT_BYTES',len(t.canonical(paged))+1)
    with pytest.raises(ValueError,match='384MiB'):t._encode_columnar(compact)
    encoded=t.encode_transport(compact); restored=t.decode_transport(encoded)
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
