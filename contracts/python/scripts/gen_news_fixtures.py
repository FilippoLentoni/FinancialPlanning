import sys,json,copy,hashlib
from pathlib import Path
p=Path(__file__).resolve().parents[2]
def sha(x):return 'sha256:'+hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def fixture(name,v,b):
 for k,x in [('valid',v),('invalid',b)]:
  f=p/'fixtures'/name/k/'synthetic.json';f.parent.mkdir(parents=True,exist_ok=True);f.write_text(json.dumps(x,indent=2)+'\n')
article={'title':'Synthetic ETF growth warning','url':'https://example.invalid/synthetic-etf','published_at':'2026-01-02T12:00:00Z','symbols':['SPY','VOO']}
payload={'records':[article]};receipt={'settlement_status':'success','amount_usdc':.011,'transaction_id':'synthetic-transaction'}
body={'provider':'synthetic-provider','query':'synthetic ETF metadata','symbols':['SPY','VOO'],'retrieved_at':'2026-01-02T13:00:00Z','payload_checksum':sha(payload),'receipt_checksum':sha(receipt),'payment_receipt':receipt,'payload':payload,'rights':{'status':'research_only','scope':'search_metadata','evidence_url':'https://example.invalid/synthetic-terms','evidence_checksum':'sha256:'+'a'*64},'quality_status':'validated','contract_version':'1.8.0'}
manifest={'record_id':sha(body),'provider':body['provider'],'query':body['query'],'symbols':body['symbols'],'retrieved_at':body['retrieved_at'],'available_at':'2026-01-02T13:01:00Z','payload_checksum':body['payload_checksum'],'receipt_checksum':body['receipt_checksum'],'rights':body['rights'],'quality_status':'validated','eligible':True,'exclusion_reasons':[],'records':[article]}
fixture('api/research-news-import-request',body,{**body,'payload_checksum':'bad'})
fixture('api/research-news-import-response',{'manifest':manifest,'contract_version':'1.8.0'},{'manifest':{},'contract_version':'1.8.0'})
response={'decision_at':'2026-01-02T14:00:00Z','symbols':['SPY','VOO'],'deliveries':[manifest],'historical_coverage':'forward_collection_only','contract_version':'1.8.0'}
fixture('api/research-news-response',response,{**response,'historical_coverage':'complete_backtest'})
fixture('tools/get-etf-research-inputs-request',{'symbols':['SPY','VOO']},{'symbols':[]})
response={'decision_at':'2026-01-02T14:00:00Z','symbols':['SPY','VOO'],'feature_protocol':'etf-search-metadata/1','features':{},'models':{},'provenance':[],'limitations':['synthetic_fixture'],'contract_version':'1.8.0'}
fixture('tools/get-etf-research-inputs-response',response,{**response,'symbols':[]})
