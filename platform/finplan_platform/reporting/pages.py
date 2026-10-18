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
    # Retain only normalized strings that need exact original-token reuse.
    # Stream all other raw tokens directly into pages, never a whole raw pool.
    wanted = {canonical(value): value for value in columnar['strings']}
    matches = {}; ranges = []
    builder_bytes = (sys.getsizeof(columnar['artifact_lexemes']) +
                     sys.getsizeof(wanted) + sum(sys.getsizeof(k)+96 for k in wanted))
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
        # Existing encoded entry + new range/list/int overhead. One raw token
        # exists transiently; PageWriter holds only1MiB uncompressed at a time.
        builder_bytes += encoded_memory(value) + 192
        if builder_bytes + sys.getsizeof(raw) > MAX_BUILDER_MEMORY_BYTES:
            raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
        if raw in wanted: matches[wanted[raw]] = index
        ranges.append([writer.append(raw), len(raw)])
    result['artifact_lexemes'] = ranges
    result['lexeme_pages'] = writer.finish()
    result['strings'] = [{'$lex': matches[value]} if value in matches else value
                         for value in columnar['strings']]
    del wanted, matches
    writer = PageWriter(reserve); chunks = []
    for cid, size, encoded in columnar['artifact_chunk_values']:
        size = integer(size, 'original index chunk size')
        if size > ARTIFACT_BYTES: raise ValueError('Original index chunk exceeds32MiB bound before expansion')
        if not isinstance(encoded,str) or len(encoded)>2*(ARTIFACT_BYTES+65536): raise ValueError('Compressed original index chunk exceeds bound')
        builder_bytes += sys.getsizeof(encoded) + sys.getsizeof(cid) + 384
        if builder_bytes > MAX_BUILDER_MEMORY_BYTES: raise ValueError('Report byte dictionary exceeds explicit1GiB builder-memory budget; no evidence truncated')
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed)>ARTIFACT_BYTES+65536:raise ValueError('Compressed original index chunk exceeds bound')
        decoder = zlib.decompressobj(16+zlib.MAX_WBITS)
        raw = decoder.decompress(compressed, ARTIFACT_BYTES+1)
        if len(raw) != size or not decoder.eof or decoder.unused_data or sha(raw) != cid:
            raise ValueError('Original index chunk checksum mismatch before paging')
        chunks.append([cid, size, writer.append(raw)])
    result['artifact_chunk_values'] = chunks
    result['index_pages'] = writer.finish()
    result['transport_codec'] = PAGE_CODEC
    result['limits'] = {**result['limits'], 'max_decoded_page_bytes': PAGE_BYTES,
                        'decoded_page_cache_bytes': PAGE_CACHE_BYTES}
    return result


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
        offset, size = tokens[index]
        pending.append((integer(offset, 'offset'), integer(size, 'size'), position))
    for offset, size, position in sorted(pending):
        text = json.loads(store.read('lexeme', offset, size))
        if not isinstance(text, str): raise ValueError('Normalized reference is not an original JSON string')
        strings[position] = text
    return strings
