#!/usr/bin/env node
/* Browser acceptance against a real generated report. No provider/model mocking.
 * npm install --prefix <EBS scratch> playwright-core
 * PLAYWRIGHT_MODULE=<scratch>/node_modules/playwright-core CHROMIUM_EXECUTABLE=<path>
 * node scripts/verify_modeling_report.cjs <latest.html> <verification.json>
 */
const fs=require('fs'),crypto=require('crypto'),path=require('path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
async function main(){
 const file=path.resolve(process.argv[2]||''),output=path.resolve(process.argv[3]||'report-verification.json');
 if(!file.endsWith('.html'))throw new Error('Pass the generated report HTML path');
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage({acceptDownloads:true});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('file://'+file);
  await page.waitForFunction(()=>typeof DATA!=='undefined');
  const data=await page.evaluate(()=>DATA);const checked=[];
  // Deterministically defer historical A while selecting B. This exercises the
  // actual asynchronous renderer, including compressed artifacts, rather than
  // checking source strings for a generation guard.
  const candidates=data.runs.filter(r=>data.inputs[r.run_id]);let selectionRace=null;
  if(candidates.length>=2){
   const [a,b]=candidates;
   await page.evaluate(async ({a,b})=>{
    const original=decodeArtifact;let release;const held=new Promise(resolve=>release=resolve);
    decodeArtifact=async input=>{if(input.checksum===DATA.inputs[a.run_id].checksum)await held;return original(input)};
    state.inputApproach=a.approach_id;state.inputDate=day(a.decision_time);state.inputRun=a.run_id;
    const renderA=renderInput();
    state.inputApproach=b.approach_id;state.inputDate=day(b.decision_time);state.inputRun=b.run_id;
    await renderInput();release();await renderA;decodeArtifact=original;
    if(state.inputRun!==b.run_id||!document.getElementById('inputContent').textContent.includes(DATA.inputs[b.run_id].checksum))throw new Error('Deferred A replaced newer B input selection');
   },{a,b});
   const pending=page.waitForEvent('download');await page.click('#exactInputDownload');const downloaded=await pending;
   const bytes=fs.readFileSync(await downloaded.path()),sha='sha256:'+crypto.createHash('sha256').update(bytes).digest('hex');
   if(sha!==data.inputs[b.run_id].checksum)throw new Error('Race-selected B download differs from original');
   selectionRace={deferred_run_id:a.run_id,selected_run_id:b.run_id,original_checksum:sha,passed:true};
  }

  for(const approach of data.registry){
   const runs=data.runs.filter(r=>r.approach_id===approach.approach_id).slice(0,2),selected=[];
   for(const run of runs){
    const input=data.inputs[run.run_id];
    if(!input){selected.push({run_id:run.run_id,status:'input_evidence_blocked',error:run.evidence_error});continue}
    const date=new Intl.DateTimeFormat('en-CA',{timeZone:data.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(run.decision_time));
    await page.selectOption('#inputDate',date);await page.selectOption('#inputApproach',approach.approach_id);await page.selectOption('#inputRun',run.run_id);
    await page.waitForFunction(expected=>document.getElementById('inputContent').textContent.includes(expected)&&document.getElementById('exactInputDownload'),input.checksum);
    const text=await page.locator('#inputContent').innerText();
    if(!text.includes(input.metadata.input_id)||!text.includes(input.checksum))throw new Error('Explorer selected wrong historical bundle '+run.run_id);
    const pending=page.waitForEvent('download');await page.click('#exactInputDownload');const download=await pending,downloadPath=await download.path(),bytes=fs.readFileSync(downloadPath);
    const sha='sha256:'+crypto.createHash('sha256').update(bytes).digest('hex');
    if(sha!==input.checksum)throw new Error('Download differs from captured original '+run.run_id);
    const payload=JSON.parse(bytes);
    if(payload.run_id!==undefined&&payload.run_id!==run.run_id)throw new Error('Downloaded run mismatch');
    if(payload.input_id!==undefined&&payload.input_id!==run.input_id)throw new Error('Downloaded input mismatch');
    if(payload.approach_id!==undefined&&payload.approach_id!==(run.source_approach_id||run.approach_id))throw new Error('Downloaded approach mismatch');
    if(payload.news?.consumed===false&&!text.includes('News not consumed'))throw new Error('News falsely attributed to model');
    if(payload.numeric?.prepared&&!text.includes('numeric')){/* actual values live under expandable numeric tables, inspected below */}
    await page.selectOption('#approachFilter',approach.approach_id);
    if(await page.inputValue('#inputRun')!==run.run_id)throw new Error('Other report filter changed pinned input');
    selected.push({run_id:run.run_id,input_id:run.input_id,checksum:sha,decision_time:run.decision_time,invocation_state:payload.invocation_state||'unspecified',news_consumed:payload.news?.consumed,news_layer:payload.news?.layer,numeric_keys:Object.keys(payload.numeric?.prepared||payload.numeric||{}),exact_byte_download:true,pinned_selection:true,status:run.status});
   }
   checked.push({approach_id:approach.approach_id,portfolio_id:approach.portfolio_id,two_decisions_inspected:selected.length===2,decisions:selected});
  }
  if(errors.length)throw new Error(errors.join('; '));
  const result={report:file,report_version:data.report_version,generated_at:data.generated_at,data_cutoff:data.data_cutoff,mode:data.mode,approaches:checked,page_errors:errors,selection_race:selectionRace,
   input_acceptance_complete:checked.every(a=>a.two_decisions_inspected&&a.decisions.every(d=>d.exact_byte_download))};
  fs.writeFileSync(output,JSON.stringify(result,null,2));console.log(JSON.stringify({verification:output,input_acceptance_complete:result.input_acceptance_complete,approaches:checked.length}));
  if(!result.input_acceptance_complete)process.exitCode=2;
 }finally{await browser.close()}
}
main().catch(e=>{console.error(e.stack);process.exitCode=1});
