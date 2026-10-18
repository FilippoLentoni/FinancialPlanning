"""Lossless original SHA/size/offset metadata in independently verified pages.

No captured content or hash is inferred. Every original 256-bit hash is retained;
unsigned canonical varints represent the already-declared numeric metadata.
"""
from __future__ import annotations

import re
from .pages import ARTIFACT_BYTES, PageStore, PageWriter, integer

ENCODING = 'sha256-u32varint/1'
MAX_ROWS = 1_000_000
MAX_U32 = 0xffffffff
MAX_RECORD_BYTES = 53


def _u32(value, name):
    value = integer(value, name)
    if value > MAX_U32:
        raise ValueError('Descriptor integer exceeds uint32 bound')
    return value


def _varint(value):
    raw = bytearray()
    while value >= 128:
        raw.append((value & 127) | 128)
        value >>= 7
    raw.append(value)
    return raw


def encode(rows, *, reserve=None):
    if len(rows) > MAX_ROWS:
        raise ValueError('Original chunk descriptor count exceeds bound')
    writer = PageWriter(reserve)
    previous = 0
    for row in rows:
        if len(row) not in (3, 6) or not isinstance(row[0], str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', row[0]):
            raise ValueError('Invalid original chunk descriptor')
        size = _u32(row[1], 'chunk size')
        offset = _u32(row[2], 'chunk offset')
        if size > ARTIFACT_BYTES or offset < previous:
            raise ValueError('Invalid original chunk size/offset order')
        raw = bytearray.fromhex(row[0][7:])
        raw.extend(_varint(size)); raw.extend(_varint(offset-previous))
        previous = offset
        raw.append(1 if len(row) == 6 else 0)
        if len(row) == 6:
            length = _u32(row[4], 'sparse patch size')
            base = _u32(row[5], 'plain base index')
            if row[3] != 'sparse-index-delta/1' or length > 12288 or base >= len(rows) or len(rows[base]) != 3:
                raise ValueError('Invalid sparse descriptor/plain base')
            raw.extend(_varint(length)); raw.extend(_varint(base))
        writer.append(raw)
    return {'encoding': ENCODING, 'count': len(rows), 'pages': writer.finish()}


def decode(table):
    if not isinstance(table, dict) or set(table) != {'encoding', 'count', 'pages'} or table['encoding'] != ENCODING:
        raise ValueError('Unknown original chunk descriptor table')
    count = integer(table['count'], 'descriptor count')
    if count > MAX_ROWS:
        raise ValueError('Original chunk descriptor count exceeds bound')
    store = PageStore(table['pages'], [])
    total = sum(page['size_bytes'] for page in table['pages'])
    if not 35*count <= total <= MAX_RECORD_BYTES*count:
        raise ValueError('Original chunk descriptor byte count exceeds bound')
    position = 0

    def read(size):
        nonlocal position
        raw = store.read('index', position, size)
        position += size
        return raw

    def number():
        value = shift = 0
        while True:
            byte = read(1)[0]
            value |= (byte & 127) << shift
            if byte & 128:
                shift += 7
                if shift > 28:
                    raise ValueError('Descriptor integer exceeds uint32 bound')
            else:
                if value > MAX_U32 or shift and byte == 0:
                    raise ValueError('Noncanonical/out-of-bound descriptor integer')
                return value

    rows = []; previous = 0
    for _ in range(count):
        cid = 'sha256:' + read(32).hex()
        size = number(); offset = previous + number(); flag = read(1)[0]
        if size > ARTIFACT_BYTES or offset > MAX_U32 or flag not in (0, 1):
            raise ValueError('Invalid original chunk descriptor values')
        previous = offset
        row = [cid, size, offset]
        if flag:
            length, base = number(), number()
            if length > 12288 or base >= count:
                raise ValueError('Sparse descriptor exceeds bounds')
            row.extend(['sparse-index-delta/1', length, base])
        rows.append(row)
    if position != total:
        raise ValueError('Trailing original chunk descriptor bytes')
    for row in rows:
        if len(row) == 6 and len(rows[row[5]]) != 3:
            raise ValueError('Sparse descriptor base must be plain; chains forbidden')
    return rows


def expand_small_offsets(tokens, encoding=None):
    if encoding is None:
        return tokens
    if encoding != 'small-range-delta/1':
        raise ValueError('Unknown original lexical offset encoding')
    result = []; previous = 0
    for token in tokens:
        if isinstance(token, list):
            if len(token) != 2:
                raise ValueError('Invalid original lexical range')
            delta = _u32(token[0], 'lexical offset delta')
            size = integer(token[1], 'lexical token size')
            previous += delta
            if previous > MAX_U32 or size > ARTIFACT_BYTES:
                raise ValueError('Original lexical range exceeds bound')
            result.append([previous, size])
        else:
            result.append(token)
    return result
