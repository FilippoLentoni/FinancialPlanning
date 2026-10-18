"""Lossless bounded self-contained report transport; original input bytes stay original."""
from __future__ import annotations
import base64
from collections import Counter
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

    Temporary gzip level1 reduces local CPU; final page/wire compression is unchanged.
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
            compressed=gzip.compress(part,compresslevel=1,mtime=0)
            pool[cid]={'encoding':'gzip+base64','checksum':cid,'size_bytes':len(part),
                       'compressed_checksum':sha(compressed),'data':base64.b64encode(compressed).decode()}
        refs.append(cid)
    return {'checksum':sha(raw),'size_bytes':len(raw),'raw_chunks':refs,'artifact_encoding':'original-byte-gzip-chunks/1'}


def unpack_bytes(capture, pool):
    if capture['size_bytes'] > MAX_ARTIFACT_BYTES: raise ValueError('Original evidence exceeds32MiB limit')
    parts = []
    for cid in capture['raw_chunks']:
        chunk = pool[cid]; compressed = base64.b64decode(chunk['data'], validate=True)
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
    if report.get('transport_codec') in (CODEC,'modeling-report-columnar/1'):
        return report
    if report.get('read_model_compacted'):
        return report
    result = dict(report); pool = {}; lexemes=Lexemes(); inputs = {}; additional = {}
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
    for field, lazy in [('runs',RUN_LAZY),('observations',OBS_LAZY)]:
        rows=[]
        for row in report[field]:
            evidence=pack(canonical(row))
            normalized={k:v for k,v in row.items() if k not in lazy and v is not None and v != [] and v != {}}
            triggers=[v for v in (row.get('trigger'),(row.get('metadata') or {}).get('trigger'),(row.get('outcome') or {}).get('trigger'),(row.get('output') or {}).get('trigger')) if v]
            if triggers: normalized['trigger']=triggers[0] if len(set(triggers))==1 else 'conflicting_trigger_evidence'
            normalized['source_evidence_ref']=evidence
            rows.append(normalized)
        result[field]=rows
    result.update(inputs=inputs,additional_inputs=additional,artifact_chunks=pool,artifact_lexemes=lexemes.values,read_model_compacted=True,
                  source_evidence_serialization='Canonical lossless full report-normalized row; input references preserve original captured bytes, not regenerated JSON.')
    return result


def _encode_columnar(report):
    """Columnar repeated row keys and interned repeated strings reduce the read model.

    This is reversible encoding, not omission. Decoder expands normalized rows;
    bulky evidence and actual model inputs remain lazy original-byte artifacts.
    """
    if report.get('transport_codec') in (CODEC,'modeling-report-columnar/1'):
        return report
    compact=compact_report(report); data=dict(compact)
    for field in ('runs','observations'):
        rows=data.pop(field); keys=sorted({k for row in rows for k in row})
        data[field+'_columns']=keys
        data[field+'_values']=[[row.get(k) for k in keys] for row in rows]
    data['input_values']=[[key,value] for key,value in data.pop('inputs').items()]
    pool=data.pop('artifact_chunks');lexemes=data.pop('artifact_lexemes',[]); counts=Counter()
    chunk_ids=list(pool);chunk_indices={cid:i for i,cid in enumerate(chunk_ids)}
    def index_refs(value):
        if isinstance(value,list):return [index_refs(v) for v in value]
        if isinstance(value,dict):
            owned=value.get('artifact_encoding') in ('original-byte-gzip-chunks/1','original-byte-json-lexemes/1') and 'checksum' in value and 'size_bytes' in value
            return {k:([chunk_indices[cid] for cid in v] if owned and k in ('raw_chunks','lexeme_chunks') else index_refs(v)) for k,v in value.items()}
        return value
    data=index_refs(data)
    chunk_values=[[cid,pool[cid]['size_bytes'],pool[cid]['data']] for cid in chunk_ids]
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
    encoded={'transport_codec':CODEC,'strings':strings,'data':encode(data),'artifact_chunk_values':chunk_values,'artifact_lexemes':lexemes,
             'limits':{'max_transport_bytes':MAX_TRANSPORT_BYTES,'max_original_artifact_bytes':MAX_ARTIFACT_BYTES,'decoded_artifact_cache_entries':2}}
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
    if encoded.get('transport_codec') not in (CODEC,'modeling-report-columnar/1'):return encoded
    strings=encoded['strings']
    def decode(value):
        if isinstance(value,list):return [decode(v) for v in value]
        if isinstance(value,dict):
            if set(value)=={'$s'}:return strings[value['$s']]
            if set(value)=={'$literal'}:return {k:decode(v) for k,v in value['$literal'].items()}
            return {k:decode(v) for k,v in value.items()}
        return value
    data=decode(encoded['data'])
    chunk_values=encoded.get('artifact_chunk_values')
    def restore_refs(value):
        if isinstance(value,list):return [restore_refs(v) for v in value]
        if isinstance(value,dict):
            owned=value.get('artifact_encoding') in ('original-byte-gzip-chunks/1','original-byte-json-lexemes/1') and 'checksum' in value and 'size_bytes' in value
            return {k:([chunk_values[i][0] for i in v] if owned and k in ('raw_chunks','lexeme_chunks') else restore_refs(v)) for k,v in value.items()}
        return value
    if chunk_values is not None:data=restore_refs(data)
    for field in ('runs','observations'):
        keys=data.pop(field+'_columns'); data[field]=[{k:v for k,v in zip(keys,row) if v is not None} for row in data.pop(field+'_values')]
    data['inputs']=dict(data.pop('input_values'));data['artifact_chunks']={cid:{'encoding':'gzip+base64','checksum':cid,'size_bytes':size,'data':value,'compressed_checksum':sha(base64.b64decode(value))} for cid,size,value in chunk_values} if chunk_values is not None else encoded['artifact_chunks'];data['artifact_lexemes']=encoded.get('artifact_lexemes',[])
    return data

# JSON lexical tokens include their ORIGINAL quote/escape/numeric/whitespace bytes.
# Dictionary compression never interprets/reformats their values.
import re
_JSON_TOKEN = re.compile(rb'"(?:\\.|[^"\\])*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null|\s+|[{}\[\],:]')


class Lexemes:
    def __init__(self): self.values=[]; self.indices={};self.encoded_indices={};self.encoded_bytes=0
    def intern(self,token):
        existing=self.indices.get(token)
        if existing is not None:return existing
        index=len(self.values);self.indices[token]=index;encoded=base64.b64encode(token).decode()
        if len(token)>256:
            compressed=base64.b64encode(gzip.compress(token,compresslevel=6,mtime=0)).decode()
            if len(compressed)+32<len(encoded): encoded=[len(token),compressed]
        self.values.append(encoded);self.encoded_bytes+=len(canonical(encoded))+1
        if self.encoded_bytes>MAX_TRANSPORT_BYTES:raise ValueError('Original lexeme evidence exceeds150MiB bound; no evidence was silently truncated')
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
    for token in source:
        position+=len(token);encoded=lookup(token)
        if encoded is None:
            encoded=bytes(_varint(intern(token)));cached[token]=encoded
        extend(encoded);tokens+=1
        if tokens%1024==0:
            refs.append(_pack_index_chunk(bytes(indices),pool));indices.clear()
    if position!=len(raw):raise ValueError('Original captured JSON bytes have an unrecognized trailing interval')
    if indices:refs.append(_pack_index_chunk(bytes(indices),pool))
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
                output.extend(_decode_lexeme(lexemes[number]));number=0;shift=0
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


def _decode_lexeme(value):
    if isinstance(value,str):return base64.b64decode(value,validate=True)
    size,encoded=value
    if size>MAX_ARTIFACT_BYTES:raise ValueError('Original lexeme exceeds artifact bound')
    import zlib
    inflater=zlib.decompressobj(16+zlib.MAX_WBITS);raw=inflater.decompress(base64.b64decode(encoded,validate=True),size+1)
    if len(raw)!=size or not inflater.eof:raise ValueError('Original lexeme expansion exceeds declared size')
    return raw


def encode_transport(report):
    if report.get('transport_codec')==GZIP_CODEC:return report
    columnar=_encode_columnar(report);raw=canonical(columnar)
    result={'transport_codec':GZIP_CODEC,'encoding':'gzip+base64','decoded_checksum':sha(raw),'decoded_size_bytes':len(raw),
            'data':base64.b64encode(gzip.compress(raw,compresslevel=6,mtime=0)).decode(),
            'limits':{'max_transport_bytes':MAX_TRANSPORT_BYTES,'max_decoded_transport_bytes':MAX_DECODED_TRANSPORT_BYTES,
                      'max_original_artifact_bytes':MAX_ARTIFACT_BYTES,'decoded_artifact_cache_bytes':64*1024*1024,'decoded_lexeme_cache_bytes':8*1024*1024}}
    size=len(canonical(result))
    if size>MAX_TRANSPORT_BYTES:raise ValueError(f'Self-contained report transport exceeds150MiB limit ({size}bytes); no evidence was silently truncated. Preserve current revision and investigate source growth.')
    return result
