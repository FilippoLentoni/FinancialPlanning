"""Matching offline CSV rows/security, including lazily preserved originals."""
import csv,io,json,subprocess
from pathlib import Path
import pytest
from finplan_platform.reporting.reports import csv_export,captured_input_bytes
from finplan_platform.reporting.transport import compact_report
from finplan_platform.reporting.managed_evidence import collect
from tests.unit.reporting.test_managed_evidence import fixture,report

@pytest.mark.parametrize('producer',['failed','jev-success','ny-midnight'])
def test_browser_csv_matches_server_rows_headers_original_lazy_values_and_formula_rules(producer):
    event=fixture(producer)['inference_evidence'];data=collect(report(event))
    data['runs']=[{'run_id':'controlled-csv-row','approach_id':event['approach_id'],'experiment_id':event['experiment_id'],'portfolio_id':event['portfolio_id'],
        'status':'failed','rationale':'\t=HYPERLINK("controlled")','target_cash':1000.0,'execution_cost':-1.25,'daily_pnl':-2.0,
        'comparison_eligible':False,'forecast':{'available':False,'reason':'controlled CSV fixture only','values':[1.0,-2.5]}}]
    data['missing_slots']=[{'run_id':'missing-controlled-slot','status':'skipped','rationale':'No model was invoked'}]
    compact=compact_report(data)
    originals={row['source_evidence_ref']['checksum']:{'payload':json.loads(captured_input_bytes(compact,row['source_evidence_ref']))}for row in compact['runs']}
    script=Path(__file__).parents[3]/'platform'/'finplan_platform'/'reporting'/'csv_export.js'
    node="""const fs=require('fs'),vm=require('vm');const input=JSON.parse(fs.readFileSync(0,'utf8'));const button={};const context={DATA:input.data,decodeArtifact:async ref=>input.originals[ref.checksum],$:()=>button,download:()=>{}};vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);context.__buildModelingCSV().then(text=>process.stdout.write(text)).catch(e=>{console.error(e);process.exitCode=1});"""
    actual=subprocess.run(['node','-e',node,str(script)],input=json.dumps({'data':compact,'originals':originals}),text=True,capture_output=True,check=True).stdout
    expected=csv_export(compact);wanted=list(csv.DictReader(io.StringIO(expected)));got=list(csv.DictReader(io.StringIO(actual)))
    assert len(got)==len(wanted)==3 and got[0].keys()==wanted[0].keys()
    for left,right in zip(wanted,got):
        for key,value in left.items():
            if value[:1]in('{','['):assert json.loads(value)==json.loads(right[key]),key
            elif key in('target_cash','execution_cost','daily_pnl')and value:assert float(value)==float(right[key]),key
            else:assert value==right[key],key
    assert got[0]['rationale'].startswith("'\t=")and got[0]['execution_cost']=='-1.25'
    assert got[0]['comparison_eligible']=='False'and got[1]['row_kind']=='managed_worker_nontrading'
    assert got[2]['run_id']=='missing-controlled-slot'


def test_historical_rerender_binds_current_embedded_client_without_mutating_caller(tmp_path):
    from tests.unit.reporting.test_modeling_reports import report as sample_report
    from finplan_platform.reporting.reports import write_report,checksum
    from finplan_platform.reporting.transport import decode_transport
    data=sample_report();data['renderer_checksum']='stale-original-renderer'
    output=write_report(data,tmp_path)
    exported=decode_transport(json.loads((Path(output['directory'])/'latest.json').read_bytes()))
    root=Path(__file__).parents[3]/'platform'/'finplan_platform'/'reporting'
    actual=checksum(b''.join((root/name).read_bytes()for name in('template.html','report.js','managed_evidence.js','page_codec.js','csv_export.js')if(root/name).exists()))
    assert exported['renderer_checksum']==actual and data['renderer_checksum']=='stale-original-renderer'
