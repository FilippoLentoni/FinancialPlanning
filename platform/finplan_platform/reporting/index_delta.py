"""Bounded one-plain-base sparse patches of original canonical token indices."""
from array import array

MAX_INDICES=1024


def varint(n):
    result=bytearray()
    while n>=128:result.append((n&127)|128);n>>=7
    result.append(n);return result


def decode(raw, *, bound=MAX_INDICES):
    values=array('I');n=shift=0
    for v in raw:
        n|=(v&127)<<shift
        if v&128:
            shift+=7
            if shift>28:raise ValueError('Index delta integer exceeds bound')
        else:
            if n>0xffffffff:raise ValueError('Index delta integer exceeds bound')
            if shift and v==0:raise ValueError('Noncanonical index bytes cannot use delta')
            values.append(n);n=shift=0
            if len(values)>bound:raise ValueError('Index delta token count exceeds bound')
    if shift:raise ValueError('Incomplete index delta integer')
    return values


def encode(values):
    out=bytearray()
    for v in values:out.extend(varint(v))
    return bytes(out)


def patch(base,target):
    if len(base)!=len(target):return None
    result=bytearray();previous=0
    for pos,value in enumerate(target):
        if value!=base[pos]:
            result.extend(varint(pos-previous));result.extend(varint(value));previous=pos
    return bytes(result)


def apply(base_raw,patch_raw):
    values=decode(base_raw)
    edits=decode(patch_raw,bound=MAX_INDICES*2)
    if len(edits)%2:raise ValueError('Incomplete sparse index patch')
    pos=0
    for i in range(0,len(edits),2):
        step,new=edits[i],edits[i+1]
        if i and not step:raise ValueError('Duplicate sparse index patch position')
        pos+=step
        if pos>=len(values):raise ValueError('Sparse index patch outside base')
        values[pos]=new
    return encode(values)
