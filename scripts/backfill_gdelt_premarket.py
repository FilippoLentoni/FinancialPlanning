"""Bounded immutable GKG2 premarket windows with explicit source coverage."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC,date,datetime,timedelta
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"platform"))
from finplan_platform.providers.gdelt_metadata import freeze_rights
from finplan_platform.providers.gdelt_premarket import freeze_day, FEATURE_VERSION

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start',type=date.fromisoformat,required=True)
    p.add_argument('--end',type=date.fromisoformat,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workers',type=int,choices=range(1,9),default=4)
    a=p.parse_args()
    if not 0 <= (a.end-a.start).days <= 1095 or a.end >= datetime.now(UTC).date():
        p.error('requires1..1096 prior calendar days')
    freeze_rights(a.output)
    days=[a.start+timedelta(days=i) for i in range((a.end-a.start).days+1)]
    rows=[];failures=[]
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures={pool.submit(freeze_day,d,a.output):d for d in days}
        for future in as_completed(futures):
            day=futures[future]
            try:
                doc=future.result()
                row={'source_day':str(day),'quality_status':doc['quality_status'],'captured_batches':doc['captured_batches'],'rejected_rows':doc['rejected_rows'],'archive_bytes':sum(s['archive_bytes'] for s in doc['lineage']['sources'])}
                rows.append(row);print(json.dumps(row),flush=True)
            except Exception as e:
                row={'source_day':str(day),'error_type':type(e).__name__,'detail':str(e)[:250]}
                failures.append(row);print(json.dumps(row),flush=True)
    report={'feature_version':FEATURE_VERSION,'start':str(a.start),'end':str(a.end),'captured_at':datetime.now(UTC).isoformat(),'expected_days':len(days),'frozen_days':len(rows),'complete_days':sum(r['quality_status']=='complete' for r in rows),'archive_bytes':sum(r['archive_bytes'] for r in rows),'windows':sorted(rows,key=lambda r:r['source_day']),'failures':failures}
    path=a.output/('capture-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')+'.json');path.write_text(json.dumps(report,indent=2));print(json.dumps({'report':str(path),'frozen_days':len(rows),'failures':len(failures)}))
    return bool(failures)
if __name__=='__main__':raise SystemExit(main())
