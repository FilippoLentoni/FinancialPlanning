"""Lossless original-byte page transport with a bounded, verified lazy reader.

Page compression never parses or regenerates captured model JSON. Original
lexical tokens and index-chunk checksums survive the representation change.
"""
from __future__ import annotations
import base64
from collections import OrderedDict
import gzip
import hashlib
import json
import sys
import zlib

PAGE_CODEC = 'modeling-report-byte-pages/1'
PAGE_BYTES = 1024 * 1024
PAGE_CACHE_BYTES = 8 * 1024 * 1024
ARTIFACT_BYTES = 32 * 1024 * 1024
# Local builder allocations, separate from unchanged wire/decoded/artifact limits.
MAX_BUILDER_MEMORY_BYTES = 1024 * 1024 * 1024


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'Invalid byte-page {name}')
    return value


class PageStore:
    """At most8MiB of decoded pages; each expansion is at most1MiB."""
    def __init__(self, index_pages, lexeme_pages):
        self.groups = {'index': index_pages, 'lexeme': lexeme_pages}
        self.cache = OrderedDict()
        self.cache_bytes = 0
        for pages in self.groups.values():
            if not isinstance(pages, list):
                raise ValueError('Byte-page group must be a list')
            for i, page in enumerate(pages):
                size = integer(page['size_bytes'], 'size')
                if size > PAGE_BYTES or i < len(pages)-1 and size != PAGE_BYTES:
                    raise ValueError('Invalid byte-page size/order')

    def page(self, group, index):
        index = integer(index, 'index')
        key = group, index
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        pages = self.groups[group]
        if index >= len(pages):
            raise ValueError('Byte-page index outside original pool')
        doc = pages[index]
        if len(doc['data']) > 2 * (PAGE_BYTES + 65536):
            raise ValueError('Compressed byte-page exceeds bound')
        compressed = base64.b64decode(doc['data'], validate=True)
        if len(compressed) > PAGE_BYTES + 65536:
            raise ValueError('Compressed byte-page exceeds bound')
        decoder = zlib.decompressobj(16+zlib.MAX_WBITS)
        raw = decoder.decompress(compressed, doc['size_bytes']+1)
        if len(raw) != doc['size_bytes'] or not decoder.eof or decoder.unused_data or sha(raw) != doc['checksum']:
            raise ValueError('Byte-page size/checksum mismatch')
        self.cache[key] = raw
        self.cache_bytes += len(raw)
        while self.cache_bytes > PAGE_CACHE_BYTES:
            _, old = self.cache.popitem(last=False)
            self.cache_bytes -= len(old)
        return raw

    def read(self, group, offset, size):
        offset = integer(offset, 'offset'); size = integer(size, 'range size')
        if size > ARTIFACT_BYTES:
            raise ValueError('Byte-page range exceeds original artifact bound')
        pages = self.groups[group]
        total = (len(pages)-1)*PAGE_BYTES + pages[-1]['size_bytes'] if pages else 0
        if offset > total or size > total-offset:
            raise ValueError('Byte-page range outside original pool')
        parts = []
        remaining = size
        while remaining:
            i, start = divmod(offset, PAGE_BYTES)
            page = self.page(group, i)
            amount = min(remaining, len(page)-start)
            if amount <= 0: raise ValueError('Empty byte-page range')
            parts.append(page[start:start+amount]); remaining -= amount; offset += amount
        return b''.join(parts)


    def token(self, value):
        if isinstance(value,list) and len(value)==2:return self.read('lexeme',*value)
        if not isinstance(value,dict) or set(value)!={'parts','size_bytes','checksum'}:
            raise ValueError('Invalid original token fragment reference')
        size=integer(value['size_bytes'],'token size')
        if size>ARTIFACT_BYTES:raise ValueError('Original token exceeds artifact bound')
        parts=value['parts']
        if not isinstance(parts,list) or not parts or len(parts)>ARTIFACT_BYTES//4096+1 or any(not isinstance(v,list) or len(v)!=2 for v in parts):
            raise ValueError('Invalid original token fragments')
        if any(not 0<integer(v[1],'fragment size')<=4096 for v in parts):raise ValueError('Invalid original token fragment size')
        if sum(integer(v[1],'fragment size')for v in parts)!=size:
            raise ValueError('Original token fragment size mismatch')
        raw=b''.join(self.read('lexeme',*v)for v in parts)
        if sha(raw)!=value['checksum']:raise ValueError('Original token fragment checksum mismatch')
        return raw


class PageChunks(dict):
    def __init__(self, values, page_store):
        super().__init__(values)
        self.page_store = page_store


class PageLexemes(list):
    def __init__(self, values, page_store):
        super().__init__(values)
        self.page_store = page_store


def packed_pages(blob):
    result = []
    for start in range(0, len(blob), PAGE_BYTES):
        raw = bytes(blob[start:start+PAGE_BYTES])
        result.append({'size_bytes': len(raw), 'checksum': sha(raw),
                       'data': base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0)).decode()})
    return result


def encoded_memory(value):
    return sys.getsizeof(value) + sum(sys.getsizeof(v) for v in value) if isinstance(value,list) else sys.getsizeof(value)


class BuilderBudget:
    """Shared allocation estimate for freshly created lexical/index dictionaries."""
    def __init__(self): self.used = 0

    def reserve(self, amount):
        self.used += amount
        if self.used > MAX_BUILDER_MEMORY_BYTES:
            raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')


class BuilderChunks(dict):
    def __init__(self, source=None, budget=None):
        super().__init__(source or {})
        self.budget = budget or BuilderBudget()
        # Existing source entries are referenced; the newly owned table is counted.
        self.budget.reserve(sys.getsizeof(self))

    def __setitem__(self, key, value):
        if key in self:
            if self[key] != value: raise ValueError('Original chunk identity cannot be overwritten')
            return
        # Fixed field names/encoding literals are module-owned constants; checksum
        # references the SAME cid string as the key. Count each new allocation once.
        fields={'encoding','checksum','size_bytes','compressed_checksum','data'}
        owned=sum(sys.getsizeof(v) for k,v in value.items() if k not in ('encoding','checksum'))
        owned+=sum(sys.getsizeof(k) for k in value if k not in fields)
        self.budget.reserve(sys.getsizeof(key) + sys.getsizeof(value) + owned)
        before=sys.getsizeof(self)
        super().__setitem__(key, value)
        self.budget.reserve(sys.getsizeof(self)-before)


class PageWriter:
    """Streaming compression: never hold the entire raw dictionary/index blob."""
    def __init__(self, reserve=None):
        self.pages=[];self.pending=bytearray();self.total=0;self.reserve=reserve

    def append(self,raw):
        offset=self.total;self.total+=len(raw);view=memoryview(raw);at=0
        while at<len(view):
            count=min(PAGE_BYTES-len(self.pending),len(view)-at)
            self.pending.extend(view[at:at+count]);at+=count
            if len(self.pending)==PAGE_BYTES:self.flush()
        return offset

    def flush(self):
        if not self.pending:return
        raw=bytes(self.pending);self.pending.clear()
        page={'size_bytes':len(raw),'checksum':sha(raw),'data':base64.b64encode(gzip.compress(raw,compresslevel=9,mtime=0)).decode()}
        if self.reserve:self.reserve(page)
        self.pages.append(page)

    def finish(self):
        self.flush();return self.pages


def page_columnar(columnar):
    """Convert legacy dictionary bytes, preserving every original chunk SHA."""
    from .transport import _decode_lexeme, canonical
    if columnar.get('transport_codec') == PAGE_CODEC: return columnar
    result = dict(columnar)
    templates=result.pop('_index_templates',{})
    # Retain only normalized strings that need exact original-token reuse.
    # Stream all other raw tokens directly into pages, never a whole raw pool.
    wanted = {canonical(value): value for value in columnar['strings']}
    matches = {}; ranges = []; fragments = {}
    builder_bytes = (sys.getsizeof(wanted) + sum(sys.getsizeof(k)+96 for k in wanted))
    def reserve(page):
        nonlocal builder_bytes
        builder_bytes += sys.getsizeof(page['data']) + 384
        if builder_bytes > MAX_BUILDER_MEMORY_BYTES:
            raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
    writer = PageWriter(reserve)
    for index, value in enumerate(columnar['artifact_lexemes']):
        if isinstance(value,str):
            if len(value)>4*((ARTIFACT_BYTES+2)//3):raise ValueError('Original lexeme exceeds32MiB bound before expansion')
        elif isinstance(value,list) and len(value)==2:
            if integer(value[0],'original lexeme size')>ARTIFACT_BYTES or not isinstance(value[1],str) or len(value[1])>2*(ARTIFACT_BYTES+65536):raise ValueError('Original lexeme exceeds32MiB bound before expansion')
        else:raise ValueError('Invalid legacy original lexeme')
        raw = _decode_lexeme(value)
        if len(raw)>ARTIFACT_BYTES:raise ValueError('Original lexeme exceeds32MiB artifact bound')
        # Existing encoded source objects are referenced, never copied here.
        # Count NEW range/list/int overhead. One raw token
        # exists transiently; PageWriter holds only1MiB uncompressed at a time.
        builder_bytes += 192
        if builder_bytes + sys.getsizeof(raw) > MAX_BUILDER_MEMORY_BYTES:
            raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
        if raw in wanted: matches[wanted[raw]] = index
        if len(raw)<4096:
            ranges.append([writer.append(raw), len(raw)])
        else:
            parts=[];builder_bytes+=384
            for start in range(0,len(raw),4096):
                part=raw[start:start+4096];key=(len(part),hashlib.sha256(part).digest())
                if key not in fragments:
                    builder_bytes+=192
                    fragments[key]=writer.append(part)
                builder_bytes+=96
                if builder_bytes+sys.getsizeof(raw)>MAX_BUILDER_MEMORY_BYTES:
                    raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
                parts.append([fragments[key],len(part)])
            ranges.append({'parts':parts,'size_bytes':len(raw),'checksum':sha(raw)})
    result['artifact_lexemes'] = ranges
    result['lexeme_pages'] = writer.finish()
    result['strings'] = [{'$lex': matches[value]} if value in matches else value
                         for value in columnar['strings']]
    del wanted, matches, fragments
    from . import index_delta
    rows={row[0]:row for row in columnar['artifact_chunk_values']}
    bases={};cache=OrderedDict();cache_bytes=0
    # Plain-base index cache is bounded independently of original-byte pages.
    cache_limit=min(16*1024*1024,MAX_BUILDER_MEMORY_BYTES//16) if templates else 0
    builder_bytes+=cache_limit+sys.getsizeof(rows)+sys.getsizeof(templates)
    builder_bytes+=sum(sys.getsizeof(v)for v in {id(v):v for v in templates.values()}.values())
    def base_values(cid):
        nonlocal cache_bytes
        if cid in cache:cache.move_to_end(cid);return cache[cid]
        source=rows[cid];decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
        raw=decoder.decompress(base64.b64decode(source[2],validate=True),source[1]+1)
        if not decoder.eof or decoder.unused_data or len(raw)!=source[1] or sha(raw)!=cid:raise ValueError('Plain delta base checksum mismatch')
        values=index_delta.decode(raw);amount=sys.getsizeof(values)+128
        cache[cid]=values;cache_bytes+=amount
        while cache_bytes>cache_limit:
            _,old=cache.popitem(last=False);cache_bytes-=sys.getsizeof(old)+128
        return values
    writer = PageWriter(reserve); chunks = []
    for cid, size, encoded in columnar['artifact_chunk_values']:
        size = integer(size, 'original index chunk size')
        if size > ARTIFACT_BYTES: raise ValueError('Original index chunk exceeds32MiB bound before expansion')
        if not isinstance(encoded,str) or len(encoded)>2*(ARTIFACT_BYTES+65536): raise ValueError('Compressed original index chunk exceeds bound')
        builder_bytes += 384
        if builder_bytes > MAX_BUILDER_MEMORY_BYTES: raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed)>ARTIFACT_BYTES+65536:raise ValueError('Compressed original index chunk exceeds bound')
        decoder = zlib.decompressobj(16+zlib.MAX_WBITS)
        raw = decoder.decompress(compressed, ARTIFACT_BYTES+1)
        if len(raw) != size or not decoder.eof or decoder.unused_data or sha(raw) != cid:
            raise ValueError('Original index chunk checksum mismatch before paging')
        context=templates.get(cid);base_cid=bases.get(context) if context else None
        candidate=None
        if base_cid:
            try:
                target=index_delta.decode(raw)
                candidate=index_delta.patch(base_values(base_cid),target)
            except ValueError:candidate=None
        if candidate is not None and len(gzip.compress(candidate,compresslevel=6,mtime=0))+64<len(compressed):
            chunks.append([cid,size,writer.append(candidate),'sparse-index-delta/1',len(candidate),base_cid])
        else:
            chunks.append([cid,size,writer.append(raw)])
            if context and context not in bases:bases[context]=cid;builder_bytes+=192

    # The original base CID already occurs in this same immutable table. Store
    # its integer reference rather than repeating a distant random SHA string.
    chunk_indices={row[0]:index for index,row in enumerate(chunks)}
    builder_bytes+=sys.getsizeof(chunk_indices)+len(chunk_indices)*96
    if builder_bytes>MAX_BUILDER_MEMORY_BYTES:raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
    for row in chunks:
        if len(row)==6:row[5]=chunk_indices[row[5]]
    result['artifact_chunk_values'] = chunks
    result['index_pages'] = writer.finish()
    result['transport_codec'] = PAGE_CODEC
    result['limits'] = {**result['limits'], 'max_decoded_page_bytes': PAGE_BYTES,
                        'decoded_page_cache_bytes': PAGE_CACHE_BYTES}
    def allocate(amount):
        nonlocal builder_bytes
        builder_bytes+=amount
        if builder_bytes>MAX_BUILDER_MEMORY_BYTES:raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
    return compact_metadata(result, reserve=reserve, allocate=allocate)


def restore_page_strings(encoded, store):
    # Resolve by original byte offset, avoiding random 1MiB page inflation while
    # retaining the exact normalized dictionary order.
    strings = [None] * len(encoded['strings'])
    tokens = encoded['artifact_lexemes']; pending = []
    for position, value in enumerate(encoded['strings']):
        if isinstance(value, str): strings[position] = value; continue
        if not isinstance(value, dict) or set(value) != {'$lex'}:
            raise ValueError('Invalid normalized byte-page string reference')
        index = integer(value['$lex'], 'lexeme index')
        if index >= len(tokens): raise ValueError('Normalized lexeme index outside pool')
        token=tokens[index]
        if isinstance(token,dict) and (not isinstance(token.get('parts'),list) or not token['parts']):raise ValueError('Invalid normalized token fragments')
        offset=token['parts'][0][0] if isinstance(token,dict) else token[0]
        pending.append((integer(offset, 'offset'), index, position))
    for offset, index, position in sorted(pending):
        text = json.loads(store.token(tokens[index]))
        if not isinstance(text, str): raise ValueError('Normalized reference is not an original JSON string')
        strings[position] = text
    return strings


def compact_metadata(columnar, *, reserve=None, allocate=None):
    """Page original descriptor bytes and delta-code monotone lexical offsets."""
    from .descriptor_table import encode
    result=dict(columnar)
    if allocate is None:
        budget=BuilderBudget();allocate=budget.reserve
        reserve=lambda page:allocate(sys.getsizeof(page['data'])+384)
    if 'artifact_chunk_table' not in result:
        rows=result['artifact_chunk_values']
        if all(rows[i][2]>=rows[i-1][2] for i in range(1,len(rows))):
            # Old JSON/string-base descriptors remain readable. Normalize those
            # references without changing either original checksum.
            normalized=rows
            if any(len(row)==6 and isinstance(row[5],str) for row in rows):
                allocate(len(rows)*192)
                indices={row[0]:i for i,row in enumerate(rows)}
                normalized=[[*row[:5],indices[row[5]]] if len(row)==6 and isinstance(row[5],str) else row for row in rows]
            result['artifact_chunk_table']=encode(normalized,reserve=reserve)
            result.pop('artifact_chunk_values')
    if 'artifact_lexeme_offsets' not in result:
        previous=0; tokens=[]
        allocate(len(result['artifact_lexemes'])*192)
        for value in result['artifact_lexemes']:
            if isinstance(value,list):
                offset,size=value; offset=integer(offset,'lexical offset');size=integer(size,'lexical token size')
                if size>ARTIFACT_BYTES:raise ValueError('Original lexical token exceeds artifact bound')
                if offset<previous or offset>0xffffffff:raise ValueError('Invalid original lexical offset order')
                tokens.append([offset-previous,size]);previous=offset
            else:tokens.append(value)
        result['artifact_lexemes']=tokens
        result['artifact_lexeme_offsets']='small-range-delta/1'
    return result
