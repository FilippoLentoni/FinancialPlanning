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
 async function token(index){integer(index,'lexeme index');const value=lexemes[index];if(!Array.isArray(value)||value.length!==2)throw new Error('Original lexeme range missing');return read('lexeme',value[0],value[1])}
 return {read,token,cacheBytes:()=>cacheBytes};
}
globalThis.__prepareReportBytePages=async function(encoded){
 if(encoded.transport_codec!=='modeling-report-byte-pages/1')return;
 const reader=createReportPageReader(encoded.index_pages,encoded.lexeme_pages,encoded.artifact_lexemes),strings=encoded.strings.slice(),needed=[];
 for(let i=0;i<strings.length;i++){const value=strings[i];if(typeof value==='string')continue;if(!value||typeof value!=='object'||Object.keys(value).length!==1||!Number.isSafeInteger(value.$lex)||value.$lex<0||!encoded.artifact_lexemes[value.$lex])throw new Error('Invalid normalized lexeme reference');needed.push([i,value.$lex])}
 // Ascending original-byte offsets avoid an unbounded/random page working set.
 needed.sort((a,b)=>encoded.artifact_lexemes[a[1]][0]-encoded.artifact_lexemes[b[1]][0]);
 for(const [i,index]of needed){const bytes=await reader.token(index),value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));if(typeof value!=='string')throw new Error('Normalized lexeme is not an original JSON string');strings[i]=value}
 encoded.strings=strings;globalThis.__reportPageReader=reader;
};
