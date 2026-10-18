'use strict';
/* Native gzip pages only. No network, code execution from evidence, or source regeneration. */
function createReportPageReader(indexPages,lexemePages,lexemes){
 const PAGE=1024*1024,CACHE=8*1024*1024,ARTIFACT=32*1024*1024,groups={index:indexPages,lexeme:lexemePages},cache=new Map(),pending=new Map();let cacheBytes=0;
 const integer=(v,name)=>{if(!Number.isSafeInteger(v)||v<0)throw new Error('Invalid byte-page '+name);return v};
 for(const pages of Object.values(groups)){if(!Array.isArray(pages))throw new Error('Invalid byte-page group');pages.forEach((p,i)=>{integer(p.size_bytes,'size');if(p.size_bytes>PAGE||(i<pages.length-1&&p.size_bytes!==PAGE))throw new Error('Invalid byte-page size/order')})}
 async function page(group,index){
  integer(index,'index');const key=group+':'+index;
  if(cache.has(key)){const raw=cache.get(key);cache.delete(key);cache.set(key,raw);return raw}
  if(pending.has(key))return pending.get(key);
  const work=(async()=>{const doc=groups[group]?.[index];if(!doc||typeof doc.data!=='string'||doc.data.length>2*(PAGE+65536))throw new Error('Compressed byte-page outside bound');const compressed=Uint8Array.from(atob(doc.data),c=>c.charCodeAt(0));if(compressed.length>PAGE+65536)throw new Error('Compressed byte-page exceeds bound');const reader=new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip')).getReader(),raw=new Uint8Array(doc.size_bytes);let at=0;while(true){const result=await reader.read();if(result.done)break;if(at+result.value.length>raw.length){await reader.cancel();throw new Error('Byte-page expansion exceeds declared size')}raw.set(result.value,at);at+=result.value.length}if(at!==raw.length)throw new Error('Byte-page size mismatch');const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',raw)),sha='sha256:'+Array.from(digest,v=>v.toString(16).padStart(2,'0')).join('');if(sha!==doc.checksum)throw new Error('Byte-page checksum mismatch');cache.set(key,raw);cacheBytes+=raw.length;while(cacheBytes>CACHE){const oldest=cache.keys().next().value;cacheBytes-=cache.get(oldest).length;cache.delete(oldest)}return raw})();pending.set(key,work);try{return await work}finally{pending.delete(key)}
 }
 async function read(group,offset,size){
  integer(offset,'offset');integer(size,'range size');if(size>ARTIFACT)throw new Error('Byte-page range exceeds original artifact bound');const pages=groups[group];if(!pages)throw new Error('Unknown byte-page group');const total=pages.length?(pages.length-1)*PAGE+pages[pages.length-1].size_bytes:0;if(offset>total||size>total-offset)throw new Error('Byte-page range outside original pool');const raw=new Uint8Array(size);let at=0;while(at<size){const index=Math.floor(offset/PAGE),start=offset%PAGE,part=await page(group,index),count=Math.min(size-at,part.length-start);if(count<=0)throw new Error('Empty byte-page range');raw.set(part.subarray(start,start+count),at);at+=count;offset+=count}return raw;
 }
 async function token(index){integer(index,'lexeme index');const value=lexemes[index];if(Array.isArray(value)&&value.length===2)return read('lexeme',value[0],value[1]);if(!value||typeof value!=='object'||Object.keys(value).sort().join(',')!=='checksum,parts,size_bytes'||!Array.isArray(value.parts))throw new Error('Original lexeme range missing');const size=integer(value.size_bytes,'token size');if(size>ARTIFACT)throw new Error('Original token exceeds artifact bound');if(value.parts.length===0||value.parts.length>Math.ceil(ARTIFACT/4096)+1)throw new Error('Too many original token fragments');let total=0;for(const part of value.parts){if(!Array.isArray(part)||part.length!==2)throw new Error('Invalid original token fragments');integer(part[0],'fragment offset');const n=integer(part[1],'fragment size');if(n===0||n>4096)throw new Error('Invalid original token fragment size');total+=n;if(total>size)throw new Error('Original token fragment size mismatch')}if(total!==size)throw new Error('Original token fragment size mismatch');const raw=new Uint8Array(size);let at=0;for(const part of value.parts){const bytes=await read('lexeme',part[0],part[1]);raw.set(bytes,at);at+=bytes.length}const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',raw)),sha='sha256:'+Array.from(digest,v=>v.toString(16).padStart(2,'0')).join('');if(sha!==value.checksum)throw new Error('Original token fragment checksum mismatch');return raw}
 return {read,token,cacheBytes:()=>cacheBytes};
}
globalThis.__prepareReportBytePages=async function(encoded){
 if(encoded.transport_codec!=='modeling-report-byte-pages/1')return;
 if(encoded.artifact_chunk_table){encoded.artifact_chunk_values=await globalThis.__decodeReportChunkTable(encoded.artifact_chunk_table);delete encoded.artifact_chunk_table}
 if(encoded.artifact_lexeme_offsets!==undefined){if(encoded.artifact_lexeme_offsets!=='small-range-delta/1')throw new Error('Unknown original lexical offset encoding');let previous=0;encoded.artifact_lexemes=encoded.artifact_lexemes.map(value=>{if(!Array.isArray(value))return value;if(value.length!==2||!Number.isSafeInteger(value[0])||value[0]<0||value[0]>0xffffffff||!Number.isSafeInteger(value[1])||value[1]<0||value[1]>32*1024*1024)throw new Error('Invalid original lexical offset delta');previous+=value[0];if(previous>0xffffffff)throw new Error('Original lexical offset exceeds bound');return[previous,value[1]]});delete encoded.artifact_lexeme_offsets}

 const reader=createReportPageReader(encoded.index_pages,encoded.lexeme_pages,encoded.artifact_lexemes),strings=encoded.strings.slice(),needed=[];
 for(let i=0;i<strings.length;i++){const value=strings[i];if(typeof value==='string')continue;if(!value||typeof value!=='object'||Object.keys(value).length!==1||!Number.isSafeInteger(value.$lex)||value.$lex<0||!encoded.artifact_lexemes[value.$lex])throw new Error('Invalid normalized lexeme reference');needed.push([i,value.$lex])}
 // Ascending original-byte offsets avoid an unbounded/random page working set.
 const offset=i=>{const value=encoded.artifact_lexemes[i];if(!Array.isArray(value)&&(!Array.isArray(value.parts)||!value.parts.length))throw new Error('Invalid normalized token fragments');return Array.isArray(value)?value[0]:value.parts[0][0]};needed.sort((a,b)=>offset(a[1])-offset(b[1]));
 for(const [i,index]of needed){const bytes=await reader.token(index),value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));if(typeof value!=='string')throw new Error('Normalized lexeme is not an original JSON string');strings[i]=value}
 encoded.strings=strings;globalThis.__reportPageReader=reader;
};

// One verified PLAIN base only; no chains, arbitrary graph traversal or recursion.
globalThis.__applyReportIndexDelta=function(base,patch){
 const encode=values=>{const out=[];for(let n of values){while(n>=128){out.push(n%128+128);n=Math.floor(n/128)}out.push(n)}return Uint8Array.from(out)};
 const decode=(raw,bound)=>{const out=[];let n=0,shift=0;for(const v of raw){n+=(v&127)*2**shift;if(v&128){shift+=7;if(shift>28)throw new Error('Index delta integer exceeds bound')}else{if(n>0xffffffff)throw new Error('Index delta integer exceeds bound');if(shift&&v===0)throw new Error('Noncanonical sparse index bytes');out.push(n);n=0;shift=0;if(out.length>bound)throw new Error('Index delta count exceeds bound')}}if(shift)throw new Error('Incomplete sparse index integer');return out};
 const values=decode(base,1024),edits=decode(patch,2048);if(edits.length%2)throw new Error('Incomplete sparse index patch');let pos=0;
 for(let i=0;i<edits.length;i+=2){if(i&&edits[i]===0)throw new Error('Duplicate sparse index position');pos+=edits[i];if(pos>=values.length)throw new Error('Sparse index position outside base');values[pos]=edits[i+1]}
 return encode(values);
};

// Metadata only: each original SHA is retained as32 bytes, never regenerated.
globalThis.__decodeReportChunkTable=async function(table){
 const MAX=1000000,ARTIFACT=32*1024*1024,PAGE=1024*1024;
 if(!table||Object.keys(table).sort().join(',')!=='count,encoding,pages'||table.encoding!=='sha256-u32varint/1'||!Number.isSafeInteger(table.count)||table.count<0||table.count>MAX)throw new Error('Invalid original descriptor table');
 const reader=createReportPageReader(table.pages,[],[]),total=table.pages.reduce((n,p)=>n+p.size_bytes,0);
 if(total<35*table.count||total>53*table.count)throw new Error('Original descriptor byte count exceeds bound');
 const hex=Array.from({length:256},(_,i)=>i.toString(16).padStart(2,'0')),rows=[];let previous=0,tail=new Uint8Array(0),buffer,pos;
 function byte(){if(pos>=buffer.length)throw new Error('Truncated original descriptor');return buffer[pos++]}
 function number(){let value=0,shift=0;while(true){const v=byte();value+=(v&127)*2**shift;if(v&128){shift+=7;if(shift>28)throw new Error('Descriptor integer exceeds uint32 bound')}else{if(value>0xffffffff||(shift&&v===0))throw new Error('Noncanonical descriptor integer');return value}}}
 function record(){if(buffer.length-pos<32)throw new Error('Truncated original descriptor SHA');const cid='sha256:'+Array.from(buffer.subarray(pos,pos+32),v=>hex[v]).join('');pos+=32;const size=number(),offset=previous+number(),flag=byte();if(size>ARTIFACT||offset>0xffffffff||flag>1)throw new Error('Invalid original descriptor values');previous=offset;const row=[cid,size,offset];if(flag){const length=number(),base=number();if(length>12288||base>=table.count)throw new Error('Sparse descriptor exceeds bounds');row.push('sparse-index-delta/1',length,base)}rows.push(row)}
 for(let i=0;i<table.pages.length;i++){const raw=await reader.read('index',i*PAGE,table.pages[i].size_bytes);buffer=new Uint8Array(tail.length+raw.length);buffer.set(tail);buffer.set(raw,tail.length);pos=0;const last=i===table.pages.length-1;while(rows.length<table.count&&(last?pos<buffer.length:buffer.length-pos>=53))record();tail=buffer.slice(pos);if(rows.length===table.count&&(tail.length||!last))throw new Error('Trailing original descriptor bytes')}
 if(rows.length!==table.count||tail.length)throw new Error('Truncated original descriptor table');
 for(const row of rows){if(row.length===6&&rows[row[5]].length!==3)throw new Error('Sparse descriptor base must be plain; chains forbidden')}
 return rows;
};
