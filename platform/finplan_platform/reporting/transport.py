"""Lossless bounded self-contained report transport; original input bytes stay original."""
from __future__ import annotations
import base64
from collections import Counter
from .pages import integer, PAGE_CODEC, PageStore, PageChunks, PageLexemes, page_columnar, restore_page_strings
import gzip
import hashlib
import json

MAX_TRANSPORT_BYTES = 150 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
CODEC = 'modeling-report-columnar/2'
GZIP_CODEC = 'modeling-report-gzip/1'
MAX_DECODED_TRANSPORT_BYTES=384*1024*1024
_GEAR = tuple(int.from_bytes(hashlib.sha256(bytes([i])).digest()[:8], 'little') for i in range(256))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def pack_bytes(raw, pool):
    """Content-defined original-byte boundaries preserve order and enable cross-run reuse.

    Never parse/re-serialize model input bytes. Boundary sizes are2..8KiB; gzip
    and every original chunk are verified in addition to the full original SHA.
    """
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError('Captured artifact exceeds explicit32MiB report input limit; evidence is not truncated')
    refs = []; start = 0; rolling = 0
    for i, byte in enumerate(raw):
        rolling = ((rolling << 1) + _GEAR[byte]) & 0xffffffffffffffff
        if i-start >= 2047 and ((rolling & 2047) == 0 or i-start >= 8191):
            part = raw[start:i+1]; cid = sha(part)
            if cid not in pool:
                compressed = gzip.compress(part, compresslevel=6, mtime=0)
                pool[cid] = {'encoding':'gzip+base64','checksum':cid,'size_bytes':len(part),
                             'compressed_checksum':sha(compressed),'data':base64.b64encode(compressed).decode()}
            refs.append(cid); start = i+1; rolling = 0
    if start < len(raw) or not refs:
        part = raw[start:]; cid = sha(part)
        if cid not in pool:
            compressed = gzip.compress(part, compresslevel=6, mtime=0)
            pool[cid] = {'encoding':'gzip+base64','checksum':cid,'size_bytes':len(part),
                         'compressed_checksum':sha(compressed),'data':base64.b64encode(compressed).decode()}
        refs.append(cid)
    return {'checksum':sha(raw),'size_bytes':len(raw),'raw_chunks':refs,'artifact_encoding':'original-byte-gzip-chunks/1'}


def pack_transient_bytes(raw, pool):
    """Fast bounded ORIGINAL-byte staging before the final lexical dictionary pass.

    Fixed64KiB chunks avoid a Python per-byte CDC pass which final compaction
    immediately discards. The portable encoding/checksums are unchanged; final
    self-contained cross-run/cohort dedup still uses original lexical bytes.
    """
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError('Captured artifact exceeds explicit32MiB report input limit; evidence is not truncated')
    refs=[]
    for start in range(0,max(1,len(raw)),65536):
        part=raw[start:start+65536];cid=sha(part)
        if cid not in pool:
            compressed=gzip.compress(part,compresslevel=6,mtime=0)
            pool[cid]={'encoding':'gzip+base64','checksum':cid,'size_bytes':len(part),
                       'compressed_checksum':sha(compressed),'data':base64.b64encode(compressed).decode()}
        refs.append(cid)
    return {'checksum':sha(raw),'size_bytes':len(raw),'raw_chunks':refs,'artifact_encoding':'original-byte-gzip-chunks/1'}


def unpack_bytes(capture, pool):
    if capture['size_bytes'] > MAX_ARTIFACT_BYTES: raise ValueError('Original evidence exceeds32MiB limit')
    parts = []; total=0
    for cid in capture['raw_chunks']:
        chunk = pool[cid]
        if chunk['size_bytes'] > capture['size_bytes']-total: raise ValueError('Original chunks exceed declared artifact size')
        total+=chunk['size_bytes']
        if chunk.get('encoding') in ('byte-page-range/1','sparse-index-delta/1'):
            if chunk['encoding']=='sparse-index-delta/1':
                from .index_delta import apply
                base=pool.get(chunk['base_chunk'])
                if not base or base.get('encoding')!='byte-page-range/1':raise ValueError('Sparse index base must be plain; chains are forbidden')
                if isinstance(chunk['encoded_size'],bool) or not isinstance(chunk['encoded_size'],int) or not 0<=chunk['encoded_size']<=12288:raise ValueError('Sparse index patch size exceeds bound')
                original=unpack_bytes({'checksum':chunk['base_chunk'],'size_bytes':base['size_bytes'],'raw_chunks':[chunk['base_chunk']]},pool)
                part=apply(original,pool.page_store.read('index',chunk['offset'],chunk['encoded_size']))
                if len(part)!=chunk['size_bytes']:raise ValueError('Sparse index original size mismatch')
            else:part = pool.page_store.read('index', chunk['offset'], chunk['size_bytes'])
            if sha(part) != cid: raise ValueError('Original index chunk checksum mismatch')
            parts.append(part)
            continue
        compressed = base64.b64decode(chunk['data'], validate=True)
        if chunk.get('compressed_checksum') and sha(compressed) != chunk['compressed_checksum']:
            raise ValueError('Compressed evidence chunk checksum mismatch')
        if chunk['size_bytes'] > MAX_ARTIFACT_BYTES: raise ValueError('Original chunk exceeds32MiB limit')
        import zlib
        inflater=zlib.decompressobj(16+zlib.MAX_WBITS)
        part=inflater.decompress(compressed, chunk['size_bytes']+1)
        if not inflater.eof or len(part)>chunk['size_bytes']: raise ValueError('Original gzip expansion exceeds declared size')
        if sha(part) != cid or len(part) != chunk['size_bytes']:
            raise ValueError('Original evidence chunk checksum mismatch')
        parts.append(part)
    raw = b''.join(parts)
    if sha(raw) != capture['checksum'] or len(raw) != capture['size_bytes']:
        raise ValueError('Complete original evidence checksum mismatch')
    return raw


def _original(capture, pool):
    if 'raw_base64' in capture:
        raw = base64.b64decode(capture['raw_base64'], validate=True)
        if sha(raw) != capture['checksum'] or len(raw) != capture['size_bytes']:
            raise ValueError('Original captured input checksum mismatch')
        return raw
    return unpack_bytes(capture, pool)


def _summary(payload):
    news = payload.get('news') or {}; numeric = payload.get('numeric') or {}
    return {k:v for k,v in {
        'invocation_state':payload.get('invocation_state'),
        'news':{k:news[k] for k in ('consumed','layer','metadata_only','text_status') if k in news},
        'numeric':{'frequency':numeric.get('frequency'),'prepared':{k:None for k in (numeric.get('prepared') or {})}},
        'holdings':{k:v for k,v in (payload.get('holdings') or {}).items() if k in ('revision','portfolio_id')},
    }.items() if v is not None}


RUN_LAZY = {'outcome','output','execution','metadata','market_session_evidence','report_key','time_key','approach_key',
            'schema_version','slot','environment','synthetic','forecast','forecast_outcome','output_ref'}
OBS_LAZY = {'payload','market_session_evidence','report_key','time_key','approach_key','observed_key','quote_provenance',
            'artifact_key','checksum','serialization','schema_version','slot','environment','size_bytes','bucket_role','corporate_action_evidence'}


def compact_report(report):
    """Keep complete normalized metrics plus lazy lossless source/evidence documents."""
    if report.get('transport_codec') in (CODEC,'modeling-report-columnar/1',PAGE_CODEC):
        return report
    if report.get('read_model_compacted'):
        return report
    from .pages import BuilderChunks
    result = dict(report); pool = BuilderChunks(); lexemes=Lexemes(page_transport=True,budget=pool.budget); inputs = {}; additional = {}
    pack=lambda raw:pack_json_bytes(raw,pool,lexemes)
    for run_id, capture in report['inputs'].items():
        raw = _original(capture, report.get('artifact_chunks', {})); payload = json.loads(raw)
        metadata = capture['metadata']
        inputs[run_id] = {**pack(raw),
            'metadata':{k:metadata[k] for k in ('input_id','captured_at','serialization','kind') if k in metadata},
            'source_metadata_ref':pack(canonical(metadata)), 'payload':_summary(payload),
            'content_summary':capture.get('content_summary',{}),'preview_is_complete':False,
            'preview_truncations':[{'path':'/','kind':'selection_summary','semantics':'Only consumption/capability summary inline; select this run to decode all original supplied values.'}]}
    for run_id, layers in report.get('additional_inputs',{}).items():
        additional[run_id] = [{**pack(_original(layer,report.get('artifact_chunks',{}))),
                              'metadata':layer.get('metadata',{}),'kind':layer['kind']} for layer in layers]
    for field, lazy in [('runs',RUN_LAZY),('observations',OBS_LAZY),('ancillary_evidence',OBS_LAZY)]:
        rows=[]
        for row in report.get(field,[]):
            evidence=pack(canonical(row))
            normalized={k:v for k,v in row.items() if k not in lazy and v is not None and v != [] and v != {}}
            triggers=[v for v in (row.get('trigger'),(row.get('metadata') or {}).get('trigger'),(row.get('outcome') or {}).get('trigger'),(row.get('output') or {}).get('trigger')) if v]
            if triggers: normalized['trigger']=triggers[0] if len(set(triggers))==1 else 'conflicting_trigger_evidence'
            normalized['source_evidence_ref']=evidence
            rows.append(normalized)
        result[field]=rows
    result.update(inputs=inputs,additional_inputs=additional,artifact_chunks=pool,artifact_lexemes=lexemes.values,read_model_compacted=True,ancillary_evidence_compacted=True,
                  source_evidence_serialization='Canonical lossless full report-normalized row; input references preserve original captured bytes, not regenerated JSON.')
    return result


def _compact_existing_ancillary(report):
    if not report.get('read_model_compacted') or report.get('ancillary_evidence_compacted'):
        return report
    result=dict(report); pool=report['artifact_chunks']
    if isinstance(pool,PageChunks):
        raise ValueError('Paged report lacks ancillary compaction marker; mixed index encodings are forbidden')
    else:
        from .pages import BuilderChunks,MAX_BUILDER_MEMORY_BYTES
        pool=BuilderChunks(pool)
    lexemes=Lexemes(page_transport=True,budget=pool.budget); source=report.get('artifact_lexemes',[])
    lexemes.values=list(source)
    # Legacy archives are transcoded without changing any original token/index.
    if isinstance(source,PageLexemes):
        lexemes.values=[base64.b64encode(source.page_store.read('lexeme',*v)).decode() for v in source]
    lexemes.indices={}
    import sys
    pool.budget.reserve(sys.getsizeof(lexemes.values)+sys.getsizeof(lexemes.indices))
    for index,value in enumerate(lexemes.values):
        raw=_decode_lexeme(value)
        key=lexemes.token_key(raw)
        pool.budget.reserve(sys.getsizeof(key)+96)
        if pool.budget.used+sys.getsizeof(raw)>MAX_BUILDER_MEMORY_BYTES:raise ValueError('Report byte dictionary exceeds builder-memory budget')
        old=lexemes.indices.get(key)
        if old is not None and _decode_lexeme(lexemes.values[old])!=raw:raise ValueError('Original token hash collision')
        lexemes.indices[key]=index
    lexemes.encoded_bytes=sum(len(canonical(v))+1 for v in lexemes.values)
    rows=[]
    for row in report.get('ancillary_evidence',[]):
        ref=pack_json_bytes(canonical(row),pool,lexemes)
        rows.append({**{k:v for k,v in row.items() if k not in OBS_LAZY and v is not None and v!=[] and v!={}},'source_evidence_ref':ref})
    result.update(ancillary_evidence=rows,artifact_chunks=pool,artifact_lexemes=lexemes.values,ancillary_evidence_compacted=True)
    return result


def _encode_columnar(report,*,measure=True,page_templates=False):
    """Columnar repeated row keys and interned repeated strings reduce the read model.

    This is reversible encoding, not omission. Decoder expands normalized rows;
    bulky evidence and actual model inputs remain lazy original-byte artifacts.
    """
    if report.get('transport_codec') in (CODEC,'modeling-report-columnar/1',PAGE_CODEC):
        return report
    compact=_compact_existing_ancillary(compact_report(report)); data=dict(compact)
    for field in ('runs','observations','ancillary_evidence'):
        rows=data.pop(field); keys=sorted({k for row in rows for k in row})
        data[field+'_columns']=keys
        data[field+'_values']=[[row.get(k) for k in keys] for row in rows]
    data['input_values']=[[key,value] for key,value in data.pop('inputs').items()]
    if page_templates:
        # Column-major values keep repeated per-approach/status patterns within
        # gzip's bounded window. Presence masks preserve missing versus null.
        for field in ('runs','observations','ancillary_evidence'):
            rows=data[field+'_values']
            data[field+'_row_count']=len(rows)
            data[field+'_layout']='columns/1'
            data[field+'_values']=[list(column) for column in zip(*rows)] if rows else [[] for _ in data[field+'_columns']]
        entries=data['input_values']; keys=sorted({key for _,value in entries for key in value})
        if len(keys)<=30:
            data.pop('input_values')
            data['input_columns']=keys
            data['input_ids']=[rid for rid,_ in entries]
            data['input_presence']=[sum(1<<i for i,key in enumerate(keys) if key in value) for _,value in entries]
            data['input_column_values']=[[value.get(key) for _,value in entries] for key in keys]
    templates={}; template_keys={}
    if page_templates:
        approaches={row['run_id']:row.get('approach_id')for row in compact['runs']}
        def bind(ref,context):
            if not isinstance(ref,dict) or ref.get('artifact_encoding')!='original-byte-json-lexemes/1':return
            for ordinal,cid in enumerate(ref.get('lexeme_chunks',[])):
                if cid not in templates:
                    key=tuple([*context,ordinal])
                    if key not in template_keys:template_keys[key]=sha(canonical(key))
                    templates[cid]=template_keys[key]
        for rid,capture in compact['inputs'].items():
            aid=approaches.get(rid,rid)
            bind(capture,[aid,'input']);bind(capture.get('source_metadata_ref'),[aid,'input_metadata'])
        for rid,layers in compact.get('additional_inputs',{}).items():
            for ordinal,layer in enumerate(layers):bind(layer,[approaches.get(rid,rid),'additional',ordinal])
        for field in ('runs','observations','ancillary_evidence'):
            for row in compact.get(field,[]):bind(row.get('source_evidence_ref'),[row.get('approach_id'),field,row.get('kind','')])
    pool=data.pop('artifact_chunks');lexemes=data.pop('artifact_lexemes',[]); counts=Counter()
    chunk_ids=list(pool);chunk_indices={cid:i for i,cid in enumerate(chunk_ids)}
    def index_refs(value):
        if isinstance(value,list):return [index_refs(v) for v in value]
        if isinstance(value,dict):
            owned=value.get('artifact_encoding') in ('original-byte-gzip-chunks/1','original-byte-json-lexemes/1') and 'checksum' in value and 'size_bytes' in value
            return {k:([chunk_indices[cid] for cid in v] if owned and k in ('raw_chunks','lexeme_chunks') else index_refs(v)) for k,v in value.items()}
        return value
    data=index_refs(data)
    paged=isinstance(pool,PageChunks)
    chunk_values=[[cid,pool[cid]['size_bytes'],pool[cid]['offset'],*([pool[cid]['encoding'],pool[cid]['encoded_size'],pool[cid]['base_chunk']]if pool[cid]['encoding']=='sparse-index-delta/1' else [])] for cid in chunk_ids] if paged else [[cid,pool[cid]['size_bytes'],pool[cid]['data']] for cid in chunk_ids]
    def visit(value):
        if isinstance(value,str) and len(value)>=20: counts[value]+=1
        elif isinstance(value,list):
            for v in value: visit(v)
        elif isinstance(value,dict):
            for v in value.values(): visit(v)
    visit(data)
    strings=sorted(v for v,n in counts.items() if n>1); indices={v:i for i,v in enumerate(strings)}
    def encode(value):
        if isinstance(value,str) and value in indices:return {'$s':indices[value]}
        if isinstance(value,list):return [encode(v) for v in value]
        if isinstance(value,dict):
            result={k:encode(v) for k,v in value.items()}
            return {'$literal':result} if set(result) in ({'$s'},{'$literal'}) else result
        return value
    encoded={'transport_codec':PAGE_CODEC if paged else CODEC,'strings':strings,'data':encode(data),'artifact_chunk_values':chunk_values,'artifact_lexemes':lexemes,
             'limits':{'max_transport_bytes':MAX_TRANSPORT_BYTES,'max_original_artifact_bytes':MAX_ARTIFACT_BYTES,'decoded_artifact_cache_entries':2}}
    if page_templates and not paged:encoded['_index_templates']=templates
    if paged:
        encoded.update(index_pages=pool.page_store.groups['index'],lexeme_pages=pool.page_store.groups['lexeme'])
        encoded['data'].pop('artifact_pages',None)
    if not measure:return encoded
    size=len(canonical(encoded))
    if size>MAX_DECODED_TRANSPORT_BYTES:
        raise ValueError(f'Decoded report transport exceeds384MiB limit ({size}bytes); no evidence was silently truncated. Preserve current revision and investigate source growth.')
    encoded['transport_size_bytes_before_size_field']=size
    return encoded


def decode_transport(encoded):
    if encoded.get('transport_codec') == GZIP_CODEC:
        import zlib
        size=encoded['decoded_size_bytes']
        if size>MAX_DECODED_TRANSPORT_BYTES:raise ValueError('Decoded report transport exceeds384MiB bound')
        inflater=zlib.decompressobj(16+zlib.MAX_WBITS);raw=inflater.decompress(base64.b64decode(encoded['data'],validate=True),size+1)
        if len(raw)!=size or not inflater.eof or sha(raw)!=encoded['decoded_checksum']:raise ValueError('Decoded report transport size/checksum mismatch')
        return decode_transport(json.loads(raw))
    if encoded.get('transport_codec') not in (CODEC,'modeling-report-columnar/1',PAGE_CODEC):return encoded
    paged=encoded.get('transport_codec')==PAGE_CODEC
    store=PageStore(encoded['index_pages'],encoded['lexeme_pages']) if paged else None
    from .descriptor_table import decode as decode_descriptors, expand_small_offsets
    tokens=expand_small_offsets(encoded.get('artifact_lexemes',[]),encoded.get('artifact_lexeme_offsets')) if paged else encoded.get('artifact_lexemes',[])
    strings=restore_page_strings({**encoded,'artifact_lexemes':tokens},store) if paged else encoded['strings']
    def decode(value):
        if isinstance(value,list):return [decode(v) for v in value]
        if isinstance(value,dict):
            if set(value)=={'$s'}:return strings[value['$s']]
            if set(value)=={'$literal'}:return {k:decode(v) for k,v in value['$literal'].items()}
            return {k:decode(v) for k,v in value.items()}
        return value
    data=decode(encoded['data'])
    chunk_values=decode_descriptors(encoded['artifact_chunk_table']) if 'artifact_chunk_table' in encoded else encoded.get('artifact_chunk_values')
    def restore_refs(value):
        if isinstance(value,list):return [restore_refs(v) for v in value]
        if isinstance(value,dict):
            owned=value.get('artifact_encoding') in ('original-byte-gzip-chunks/1','original-byte-json-lexemes/1') and 'checksum' in value and 'size_bytes' in value
            return {k:([chunk_values[i][0] for i in v] if owned and k in ('raw_chunks','lexeme_chunks') else restore_refs(v)) for k,v in value.items()}
        return value
    if chunk_values is not None:data=restore_refs(data)
    for field in ('runs','observations','ancillary_evidence'):
        if field+'_columns' not in data: continue
        keys=data.pop(field+'_columns'); values=data.pop(field+'_values')
        layout=data.pop(field+'_layout',None)
        if layout is not None:
            if layout!='columns/1':raise ValueError('Unknown report row layout')
            count=integer(data.pop(field+'_row_count'),'row count')
            if len(values)!=len(keys) or any(not isinstance(column,list) or len(column)!=count for column in values):raise ValueError('Invalid report column lengths')
            columns=values
            values=([column[i] for column in columns] for i in range(count))
        data[field]=[{k:v for k,v in zip(keys,row) if v is not None} for row in values]
    if 'input_columns' in data:
        keys=data.pop('input_columns'); ids=data.pop('input_ids'); masks=data.pop('input_presence'); values=data.pop('input_column_values')
        if len(keys)>30 or len(masks)!=len(ids) or len(values)!=len(keys) or any(not isinstance(column,list) or len(column)!=len(ids) for column in values):raise ValueError('Invalid input column lengths')
        data['inputs']={}
        for i,rid in enumerate(ids):
            mask=integer(masks[i],'input presence mask')
            if mask>=(1<<len(keys)):raise ValueError('Invalid input presence mask')
            data['inputs'][rid]={key:values[k][i] for k,key in enumerate(keys) if mask&(1<<k)}
    else:data['inputs']=dict(data.pop('input_values'))
    if paged:
        values={}
        for row in chunk_values:
            cid,size,offset=row[:3]
            values[cid]={'encoding':'byte-page-range/1','checksum':cid,'size_bytes':size,'offset':offset}
            if len(row)>3:
                if len(row)!=6 or row[3]!='sparse-index-delta/1':raise ValueError('Invalid sparse index descriptor')
                base=row[5]
                if not isinstance(base,str):
                    base=integer(base,'plain base chunk index')
                    if base>=len(chunk_values):raise ValueError('Plain base chunk index outside pool')
                    base=chunk_values[base][0]
                values[cid].update(encoding=row[3],encoded_size=row[4],base_chunk=base)
        data['artifact_chunks']=PageChunks(values,store)
        data['artifact_lexemes']=PageLexemes(tokens,store)
        data['artifact_pages']={'index_pages':encoded['index_pages'],'lexeme_pages':encoded['lexeme_pages']}
    else:
        data['artifact_chunks']={cid:{'encoding':'gzip+base64','checksum':cid,'size_bytes':size,'data':value,'compressed_checksum':sha(base64.b64decode(value))} for cid,size,value in chunk_values} if chunk_values is not None else encoded['artifact_chunks']
        data['artifact_lexemes']=encoded.get('artifact_lexemes',[])
    return data

# JSON lexical tokens include their ORIGINAL quote/escape/numeric/whitespace bytes.
# Dictionary compression never interprets/reformats their values.
import re
_JSON_TOKEN = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"|[{}\[\],:]|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null|\s+')


class Lexemes:
    def __init__(self,*,page_transport=False,budget=None):
        self.values=[]; self.indices={};self.encoded_indices={};self.encoded_bytes=0
        self.page_transport=page_transport;self.builder_bytes=0;self.budget=budget
        from collections import OrderedDict
        self.hot=OrderedDict();self.hot_bytes=0;self.hot_limit=0
        if page_transport:
            from .pages import MAX_BUILDER_MEMORY_BYTES
            self.hot_limit=min(8*1024*1024,MAX_BUILDER_MEMORY_BYTES//16)
            self.builder_bytes=self.hot_limit
            if budget:budget.reserve(self.hot_limit)

    def token_key(self,token):
        return hashlib.sha256(token).digest() if self.page_transport else token

    def intern(self,token):
        import sys
        if self.page_transport:
            hot=self.hot.get(token)
            if hot is not None:
                self.hot.move_to_end(token)
                index=0;shift=0
                for byte in hot:
                    index|=(byte&127)<<shift;shift+=7
                return index
        key=self.token_key(token);existing=self.indices.get(key)
        if existing is not None:
            if self.page_transport and _decode_lexeme(self.values[existing])!=token:
                raise ValueError('Original token hash collision; evidence cannot be aliased')
            index=existing
        else:
            index=len(self.values);encoded=base64.b64encode(token).decode()
            if len(token)>256:
                compressed=base64.b64encode(gzip.compress(token,compresslevel=6,mtime=0)).decode()
                if len(compressed)+32<len(encoded): encoded=[len(token),compressed]
            if self.page_transport:
                from .pages import MAX_BUILDER_MEMORY_BYTES,encoded_memory
                allocation=sys.getsizeof(key)+encoded_memory(encoded)+256
                self.builder_bytes+=allocation
                if self.budget:self.budget.reserve(allocation)
                used=self.budget.used if self.budget else self.builder_bytes
                if used+sys.getsizeof(token)>MAX_BUILDER_MEMORY_BYTES:
                    raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
            self.indices[key]=index;self.values.append(encoded);self.encoded_bytes+=len(canonical(encoded))+1
            if not self.page_transport and self.encoded_bytes>MAX_TRANSPORT_BYTES:
                raise ValueError('Original lexeme evidence exceeds150MiB bound; no evidence was silently truncated')
        if self.page_transport and len(token)<=64:
            # One combined bounded lookaside: exact bytes -> verified varint.
            # Encoded bytes and the OrderedDict entry share this8MiB budget.
            amount=sys.getsizeof(token)+192
            if amount<=self.hot_limit:
                encoded=self.encoded_indices.get(index)
                if encoded is None:
                    encoded=bytes(_varint(index));self.encoded_indices[index]=encoded
                self.hot[token]=encoded;self.hot_bytes+=amount
                while self.hot_bytes>self.hot_limit:
                    old,_=self.hot.popitem(last=False);self.hot_bytes-=sys.getsizeof(old)+192
        return index


def _varint(number):
    result=bytearray()
    while number>=128:result.append((number&127)|128);number>>=7
    result.append(number);return result


def pack_json_bytes(raw,pool,lexemes):
    if len(raw)>MAX_ARTIFACT_BYTES:raise ValueError('Captured artifact exceeds explicit32MiB report input limit; evidence is not truncated')
    refs=[];indices=bytearray();position=0;tokens=0
    cached=lexemes.encoded_indices;lookup=cached.get;extend=indices.extend;intern=lexemes.intern
    # C-level findall avoids one Python Match object/method sequence per token.
    # Bound its temporary list to1MiB of original bytes; larger artifacts stream.
    source=_JSON_TOKEN.findall(raw) if len(raw)<=1024*1024 else (m.group() for m in _JSON_TOKEN.finditer(raw))
    if lexemes.page_transport:
        from itertools import islice
        hot_get=lexemes.hot.get;hot_item=lexemes.hot.__getitem__
        def encode_token(token):
            # Byte-key equality proves a hit is the previously verified token.
            # Cold entries still pass intern's full SHA/equality collision guard.
            # FIFO access avoids per-token LRU mutation; eviction stays bounded.
            hot=hot_get(token)
            if hot is not None:return hot
            index=intern(token);encoded=lookup(index)
            if encoded is None:
                encoded=bytes(_varint(index));cached[index]=encoded
            return encoded
        iterator=iter(source)
        while True:
            batch=list(islice(iterator,1024))
            if not batch:break
            position+=sum(map(len,batch))
            # Same 1024-token original chunk boundaries; temporary list <=1024.
            try:
                encoded=b''.join(map(hot_item,batch))
            except KeyError:
                # Probe never changes indices. New tokens are interned strictly
                # in first appearance order; local references stay <=1024.
                local={token:encode_token(token)for token in dict.fromkeys(batch)}
                encoded=b''.join(map(local.__getitem__,batch))
            refs.append(_pack_index_chunk(encoded,pool))
    else:
        for token in source:
            position+=len(token)
            encoded=lookup(token)
            if encoded is None:
                encoded=bytes(_varint(intern(token)));cached[token]=encoded
            extend(encoded);tokens+=1
            if tokens%1024==0:
                refs.append(_pack_index_chunk(bytes(indices),pool));indices.clear()
        if indices:refs.append(_pack_index_chunk(bytes(indices),pool))
    if position!=len(raw):raise ValueError('Original captured JSON bytes have an unrecognized trailing interval')
    return {'checksum':sha(raw),'size_bytes':len(raw),'lexeme_chunks':refs,'artifact_encoding':'original-byte-json-lexemes/1'}


def unpack_json_bytes(capture,pool,lexemes):
    if capture.get('artifact_encoding')!='original-byte-json-lexemes/1':return unpack_bytes(capture,pool)
    if capture['size_bytes']>MAX_ARTIFACT_BYTES:raise ValueError('Original evidence exceeds32MiB limit')
    output=bytearray()
    for ref in capture['lexeme_chunks']:
        chunk=pool[ref];indices=unpack_bytes({'checksum':ref,'size_bytes':chunk['size_bytes'],'raw_chunks':[ref]},pool);number=0;shift=0
        for value in indices:
            number|=(value&127)<<shift
            if value&128:
                shift+=7
                if shift>35:raise ValueError('Invalid lexeme index')
            else:
                output.extend(_decode_lexeme(lexemes[number],getattr(lexemes,'page_store',None)));number=0;shift=0
                if len(output)>capture['size_bytes']:raise ValueError('Lexeme expansion exceeds original artifact bound')
        if shift:raise ValueError('Incomplete lexeme index')
    raw=bytes(output)
    if len(raw)!=capture['size_bytes'] or sha(raw)!=capture['checksum']:raise ValueError('Original lexeme input checksum mismatch')
    return raw


def _pack_index_chunk(raw,pool):
    cid=sha(raw)
    if cid not in pool:
        compressed=gzip.compress(raw,compresslevel=6,mtime=0)
        pool[cid]={'encoding':'gzip+base64','checksum':cid,'size_bytes':len(raw),'compressed_checksum':sha(compressed),'data':base64.b64encode(compressed).decode()}
    return cid


def _decode_lexeme(value,page_store=None):
    if page_store is not None:
        return page_store.token(value)
    if isinstance(value,str):
        if len(value)>4*((MAX_ARTIFACT_BYTES+2)//3):raise ValueError('Original lexeme exceeds artifact bound')
        raw=base64.b64decode(value,validate=True)
        if len(raw)>MAX_ARTIFACT_BYTES:raise ValueError('Original lexeme exceeds artifact bound')
        return raw
    if not isinstance(value,list) or len(value)!=2:raise ValueError('Invalid original lexeme')
    size,encoded=value
    if isinstance(size,bool) or not isinstance(size,int) or size<0 or size>MAX_ARTIFACT_BYTES:raise ValueError('Original lexeme exceeds artifact bound')
    if not isinstance(encoded,str) or len(encoded)>2*(MAX_ARTIFACT_BYTES+65536):raise ValueError('Compressed original lexeme exceeds bound')
    compressed=base64.b64decode(encoded,validate=True)
    if len(compressed)>MAX_ARTIFACT_BYTES+65536:raise ValueError('Compressed original lexeme exceeds bound')
    import zlib
    inflater=zlib.decompressobj(16+zlib.MAX_WBITS);raw=inflater.decompress(compressed,size+1)
    if len(raw)!=size or not inflater.eof or inflater.unused_data:raise ValueError('Original lexeme expansion exceeds declared size')
    return raw


def encode_transport(report):
    if report.get('transport_codec')==GZIP_CODEC:return report
    from .pages import compact_metadata
    columnar=compact_metadata(page_columnar(_encode_columnar(report,measure=False,page_templates=True)));raw=canonical(columnar)
    if len(raw)>MAX_DECODED_TRANSPORT_BYTES:raise ValueError(f'Decoded report transport exceeds384MiB limit ({len(raw)}bytes); no evidence was silently truncated. Preserve current revision and investigate source growth.')
    result={'transport_codec':GZIP_CODEC,'encoding':'gzip+base64','decoded_checksum':sha(raw),'decoded_size_bytes':len(raw),
            'data':base64.b64encode(gzip.compress(raw,compresslevel=9,mtime=0)).decode(),
            'limits':{'max_transport_bytes':MAX_TRANSPORT_BYTES,'max_decoded_transport_bytes':MAX_DECODED_TRANSPORT_BYTES,
                      'max_original_artifact_bytes':MAX_ARTIFACT_BYTES,'decoded_artifact_cache_bytes':64*1024*1024,'decoded_lexeme_cache_bytes':8*1024*1024}}
    size=len(canonical(result))
    if size>MAX_TRANSPORT_BYTES:raise ValueError(f'Self-contained report transport exceeds150MiB limit ({size}bytes); no evidence was silently truncated. Preserve current revision and investigate source growth.')
    return result
