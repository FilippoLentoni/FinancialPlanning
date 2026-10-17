from pathlib import Path
import json
from datetime import datetime,timezone,timedelta
UTC=timezone.utc
root=Path(__file__).resolve().parents[2]
def fixture(name,valid,invalid):
 for kind,doc in [('valid',valid),('invalid',invalid)]:
  p=root/'fixtures'/name/kind/'synthetic.json';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(doc,indent=2)+'\n')
end=datetime(2026,1,2,tzinfo=UTC)
rows=[{'start':(end-timedelta(hours=24-i)).isoformat().replace('+00:00','Z'),'open':100,'close':101,'low':99,'high':102,'volume':10} for i in range(24)]
d={'dataset_id':'coinbase-crypto-hourly-v1','as_of':'2026-01-02T00:00:00Z','available_at':'2026-01-02T00:05:00Z','granularity_seconds':3600,'market_data_synthetic':True,'products':['BTC-USD','ETH-USD'],'bars':{'BTC-USD':rows,'ETH-USD':rows},'sources':[],'dataset_checksum':'sha256:'+'a'*64,'contract_version':'1.7.0'}
fixture('api/crypto-dataset-response',d,{**d,'granularity_seconds':86400})
for name,kind,payload in [('get-crypto-experiment','crypto_experiment','experiment'),('recommend-crypto-portfolio','crypto_recommendation','recommendation'),('list-crypto-history','crypto_history','history'),('discover-research-data','research_discovery','discovery')]:
 fixture('tools/'+name+'-request',{'synthetic':True},{'synthetic':True,'execution_mode':'live'})
 aid='ca_'+'a'*32
 response={'analysis_id':aid,'analysis_kind':kind,'created_at':'2026-01-02T00:10:00Z','summary':'Synthetic contract fixture only.','contract_version':'1.7.0','synthetic':True,'analysis_ref':{'artifact_id':aid,'owner':'financemodel','kind':'classical_analysis','checksum':'sha256:'+'a'*64,'content_type':'application/json'},payload:[] if payload=='history' else {}}
 fixture('tools/'+name+'-response',response,{**response,'analysis_id':'invalid'})
