/* Matching server CSV data; original rows are decoded one at a time on demand. */
(()=>{
 const fields=["row_kind","event_id","managed_run_id","origin_run_id","origin_input_id","evidence_status","original_artifact_count","cost_event_id","worker_occurred_at","experiment_id","source_approach_id","run_id","approach_id","portfolio_id","decision_time","scheduled_slot","input_id","availability_cutoff","status","target_weights","target_cash","rationale","forecast","execution_cost","model_cost","managed_cost_event","fills","fill_status","fill_time","execution_status","execution_disposition_at","execution_disposition_ref","execution_disposition","comparison_eligible","comparison_exclusion_code","comparison_exclusion_reason","portfolio_value_at_decision","daily_pnl","daily_return","valuation_id","opening_valuation_id","expected_value","forecast_target","forecast_status","realized_value","signed_error","evidence_error"];
 function cell(value){
  if(value===null||value===undefined)return '""';
  let text=typeof value==='object'?JSON.stringify(value):typeof value==='boolean'?(value?'True':'False'):String(value);
  if(typeof value==='string'&&['=','+','-','@','\t','\r'].includes(value[0]))text="'"+text;
  return '"'+text.replace(/"/g,'""')+'"';
 }
 function worker(event){const policy=event.policy||{};return {row_kind:event.outcome_update_only?'managed_worker_outcome_update':'managed_worker_nontrading',event_id:event.event_id,run_id:event.event_id,managed_run_id:event.managed_run_id,origin_run_id:event.origin_run_id,origin_input_id:event.origin_input_id,experiment_id:event.experiment_id,approach_id:event.approach_id,source_approach_id:('source_approach_id'in event)?event.source_approach_id:event.approach_id,portfolio_id:event.portfolio_id,decision_time:event.decision_time,worker_occurred_at:event.occurred_at,scheduled_slot:event.scheduled_slot,availability_cutoff:event.availability_cutoff,status:event.job_state,evidence_status:event.evidence_status,original_artifact_count:event.artifact_summaries.length,cost_event_id:event.cost_event_id,target_weights:policy.weights,target_cash:policy.cash,rationale:policy.rationale,forecast:policy.forecast,comparison_eligible:false,fill_status:'nontrading_managed_inference_only',comparison_exclusion_reason:'Original managed worker output; only a later eligible hourly decision may use it'}}
 async function build(){const lines=[fields.map(cell).join(',')];
  const append=row=>lines.push(fields.map(k=>cell(row[k])).join(','));
  for(const row of DATA.runs){let source=row.source_evidence_ref?(await decodeArtifact(row.source_evidence_ref)).payload:row;append(source)}
  for(const event of DATA.managed_inference_events||[])append(worker(event));
  for(const missing of DATA.missing_slots||[])append(missing);
  return lines.join('\r\n')+'\r\n';
 }
 globalThis.__buildModelingCSV=build;
 $('downloadCSV').onclick=async()=>{const button=$('downloadCSV'),label=button.textContent;button.disabled=true;button.textContent='Verifying CSV evidence…';try{const text=await build();download(`modeling-${DATA.start_day}-${DATA.end_day}.csv`,text,'text/csv');button.textContent=label}catch(error){button.textContent='CSV integrity failure: '+error.message}finally{button.disabled=false}};
})();
