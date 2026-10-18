"""Exact metadata preservation and bounded Python/browser descriptor readers."""
import copy
import json
import shutil
import subprocess
from pathlib import Path
import pytest
from finplan_platform.reporting import descriptor_table as d
from finplan_platform.reporting import pages as p


def rows(count=3):
    return [['sha256:'+f'{i:064x}', 1+i%6000, i*7000] for i in range(count)]


def test_original_hash_bits_metadata_cross_page_and_browser_are_identical():
    original=rows(30000)
    original[25000].extend(['sparse-index-delta/1',5,0])
    encoded=d.encode(original)
    assert len(encoded['pages'])>1 and d.decode(encoded)==original
    node=shutil.which('node')
    if not node:pytest.skip('Node is needed for cross-language metadata proof')
    script="const fs=require('node:fs'),vm=require('node:vm');vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));__decodeReportChunkTable(JSON.parse(fs.readFileSync(0,'utf8'))).then(rows=>process.stdout.write(JSON.stringify(rows))).catch(error=>{console.error(error);process.exit(1)});"
    actual=json.loads(subprocess.run([node,'-e',script,str(Path(p.__file__).with_name('page_codec.js'))],input=json.dumps(encoded),text=True,capture_output=True,check=True).stdout)
    assert actual==original


def test_malformed_metadata_fails_without_unbounded_expansion():
    encoded=d.encode(rows())
    for count in (True,-1,d.MAX_ROWS+1):
        bad={**encoded,'count':count}
        with pytest.raises(ValueError):d.decode(bad)
    bad=copy.deepcopy(encoded);bad['pages'][0]['checksum']='sha256:'+'0'*64
    with pytest.raises(ValueError,match='checksum'):d.decode(bad)
    for raw in (bytes(32)+b'\x81\x00\x00\x00',bytes(32)+b'\x01\x00\x02',bytes(32)+b'\x01\x00\x00\x00'):
        bad={'encoding':d.ENCODING,'count':1,'pages':p.packed_pages(raw)}
        with pytest.raises(ValueError):d.decode(bad)
    malformed=rows(2);malformed[1].extend(['sparse-index-delta/1',1,1])
    with pytest.raises(ValueError,match='plain base'):d.encode(malformed)
    raw=bytes(32)+b'\x01\x00\x01\x01\x00'
    with pytest.raises(ValueError,match='plain|chains'):d.decode({'encoding':d.ENCODING,'count':1,'pages':p.packed_pages(raw)})


def test_small_offsets_keep_fragment_offsets_absolute_and_reject_invalid_sums():
    tokens=[[10,2],{'parts':[[0,2]],'size_bytes':2,'checksum':'fixture'},[5,3]]
    assert d.expand_small_offsets(tokens,'small-range-delta/1')==[[10,2],tokens[1],[15,3]]
    assert d.expand_small_offsets(tokens) is tokens
    for bad in ([[True,1]], [[-1,1]], [[0xffffffff,1],[1,1]], [[1,p.ARTIFACT_BYTES+1]]):
        with pytest.raises(ValueError):d.expand_small_offsets(bad,'small-range-delta/1')
