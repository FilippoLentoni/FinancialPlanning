/* Independent managed attempts preserve original worker data, never extra trades. */
(()=>{
 const events=DATA.managed_inference_events||[];if(!events.length)return;
 const panel=document.createElement('section');panel.className='panel';panel.id='managedWorkerExplorer';
 panel.innerHTML='<h2>Managed worker actual input & outcome evidence</h2><p class="notice">Original approved-worker attempts, including failed and partial runs. These records are not extra primary hourly trading opportunities. Approved bindings and preflight requests are distinct from worker model inputs; a captured before-invocation input without an original response does not prove successful consumption.</p><div class="row"><label>Worker outcome date<select id="managedDate"></select></label><label>Model / approach<select id="managedApproach"></select></label><label>Worker decision / attempt<select id="managedRun"></select></label><label>Original artifact / role<select id="managedArtifact"></select></label></div><div id="managedEvidenceTable" class="scroll"></div><div id="managedInputContent"></div>';
 document.getElementById('inputExplorer').after(panel);
 let selected=events[0],index=0,generation=0;
 const date=at=>day(at),approaches=[...new Set(events.map(e=>e.approach_id))];
 options($('managedDate'),[...new Set(events.map(e=>date(e.occurred_at)))].sort().map(v=>[v,v]),date(selected.occurred_at));
 options($('managedApproach'),approaches.map(v=>[v,model(v).display_name||v]),selected.approach_id);
 function runs(){const rows=events.filter(e=>date(e.occurred_at)===$('managedDate').value&&e.approach_id===$('managedApproach').value);if(!rows.some(e=>e.event_id===selected.event_id))selected=rows[0];options($('managedRun'),rows.map(e=>[e.event_id,e.occurred_at+' · '+e.job_state+' · '+e.managed_run_id]),selected?.event_id);index=0;artifacts()}
 function artifacts(){const values=selected?.artifact_summaries||[];options($('managedArtifact'),values.map((e,i)=>[String(i),e.kind+(e.role?' · '+e.role:'')+' · '+e.consumption_status]),String(index));render()}
 async function render(){const mine=++generation,event=selected,artifact=index;
  $('managedEvidenceTable').innerHTML=table(['Approach / original worker','Actual outcome time / status','Original context / evidence','Policy / trading'],(event?[event]:[]).map(e=>[esc(model(e.approach_id).display_name)+'<p>'+esc(e.managed_run_id)+'</p>',esc(e.occurred_at)+'<p>'+esc(e.job_state)+' / '+esc(e.evidence_status)+'</p>',esc(e.origin_run_id)+'<br>'+esc(e.origin_input_id)+'<p>Original scheduled slot '+esc(e.scheduled_slot)+' · availability cutoff '+esc(e.availability_cutoff)+'</p><p>'+e.artifact_summaries.length+' original artifacts · '+esc(e.binding_checksum)+'</p>',e.policy?preview(e.policy):'No final policy captured']));
  if(!event){$('managedInputContent').textContent='No managed attempt matches these selectors.';return}
  const layers=DATA.additional_inputs?.[event.event_id]||[],summary=event.artifact_summaries[artifact];
  if(!summary){$('managedInputContent').innerHTML='<div class="notice">Original worker artifacts are unavailable or indeterminate. No input was reconstructed from today’s book or preflight request.</div>'+preview(event);return}
  $('managedInputContent').textContent='Verifying exact original worker bytes…';
  try{const decoded=await decodeArtifact(layers[artifact]);if(mine!==generation||event!==selected||artifact!==index)return;
   $('managedInputContent').innerHTML='<h3>'+esc(summary.kind)+' · '+esc(summary.role||'')+(summary.call_index==null?'':' · call '+esc(summary.call_index))+'</h3><p class="notice">'+esc(summary.consumption_status)+' · Worker outcome '+esc(event.job_state)+'. No automatic primary fill is implied.</p><p class="evidence">'+esc(event.event_id)+'<br>'+esc(summary.artifact_id)+'<br>'+esc(summary.checksum)+' · '+summary.size_bytes+' original bytes</p><button id="managedExactDownload">Download exact original worker artifact</button>'+preview(decoded.payload)+'<details><summary>Complete original JSON / lexical bytes</summary><pre>'+esc(new TextDecoder().decode(decoded.bytes))+'</pre></details>';
   $('managedExactDownload').onclick=()=>download(summary.artifact_id+'.json',decoded.bytes,'application/json');
  }catch(error){if(mine===generation)$('managedInputContent').textContent='Original managed evidence integrity failure: '+error.message}
 }
 $('managedDate').onchange=runs;$('managedApproach').onchange=runs;$('managedRun').onchange=()=>{selected=events.find(e=>e.event_id===$('managedRun').value);index=0;artifacts()};$('managedArtifact').onchange=()=>{index=Number($('managedArtifact').value);render()};runs();
})();
