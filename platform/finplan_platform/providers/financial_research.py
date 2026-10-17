"""Platform-owned bounded dated S&P research retrieval, no model or brokerage invocation."""
from __future__ import annotations
import concurrent.futures
import hashlib
import io
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC,datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from html.parser import HTMLParser
from finplan_contracts.canonical import canonicalize as canonical_json_bytes
from finplan_platform.core.artifacts import sha256_checksum
VERSION='finplan-financial-research/1'
CONSTITUENTS_URL='https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
HOLDINGS_URL='https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx'
MAX_BYTES=2_000_000

def stamp():return datetime.now(UTC).isoformat().replace('+00:00','Z')

def fetch(url,*,timeout=15):
    request=urllib.request.Request(url,headers={'User-Agent':'FinanceResearch/1 public bounded research'})
    with urllib.request.urlopen(request,timeout=timeout) as response:
        data=response.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES:raise ValueError('public source exceeded byte bound')
    return data


def universe(data,retrieved_at):
    """Official fund holdings, explicitly distinguished from index-provider membership.

    As-of is source supplied. Effective membership date is unknown; this artifact cannot
    prove historical S&P index inclusion and is never backdated for replay.
    """
    ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(i.file_size for i in archive.infolist())>20_000_000:raise ValueError('universe archive exceeds bound')
        strings=[''.join(s.itertext()) for s in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('m:si',ns)]
        sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    rows=[]
    for row in sheet.findall('.//m:row',ns):
        values={}
        for cell in row:
            v=cell.find('m:v',ns)
            if v is not None:values[re.sub(r'\d','',cell.attrib['r'])]=strings[int(v.text)] if cell.attrib.get('t')=='s' else v.text
        rows.append(values)
    asof=next(r['B'].removeprefix('As of ') for r in rows if r.get('A')=='Holdings:')
    asof=datetime.strptime(asof,'%d-%b-%Y').date().isoformat()
    members=[{'symbol':r['B'],'name':r['A'],'sector':r.get('F'), 'weight_percent':float(r['E']),
              'source_as_of':asof,'membership_effective_at':None} for r in rows
             if r.get('B') and re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,11}',r['B']) and r.get('H')=='USD' and r.get('E')]
    if not 490<=len(members)<=520:raise ValueError('unexpected official holdings universe size')
    return {'provider':'State Street SPDR','source_url':HOLDINGS_URL,'source_as_of':asof,'retrieved_at':retrieved_at,
            'available_at':retrieved_at,'checksum':sha256_checksum(data),'members':members,
            'scope':'official_SPY_equity_holdings_proxy_for_SP500_research',
            'historical_eligibility':'forward_collection_only','membership_effective_at':None,
            'coverage_blocker':'Index-provider constituent membership/effective dates not supplied; official fund holdings are a labeled proxy'}


class ConstituentParser(HTMLParser):
    def __init__(self):
        super().__init__();self.active=False;self.depth=0;self.cell=False;self.row=[];self.rows=[];self.parts=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='table':
            if attrs.get('id')=='constituents':self.active=True
            if self.active:self.depth+=1
        if self.active and tag in ('td','th'):self.cell=True;self.parts=[]
        if self.active and tag=='tr':self.row=[]
    def handle_data(self,data):
        if self.active and self.cell:self.parts.append(data)
    def handle_endtag(self,tag):
        if self.active and tag in ('td','th'):
            self.row.append(' '.join(''.join(self.parts).split()));self.cell=False
        if self.active and tag=='tr' and self.row:self.rows.append(self.row)
        if self.active and tag=='table':
            self.depth-=1
            if self.depth==0:self.active=False


def constituent_universe(raw,retrieved_at):
    parser=ConstituentParser();parser.feed(raw.decode('utf-8'))
    if not parser.rows or parser.rows[0][0]!='Symbol':raise ValueError('published constituent table missing')
    header=parser.rows[0];members=[]
    for row in parser.rows[1:]:
        values=dict(zip(header,row));symbol=values.get('Symbol','')
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,11}',symbol):raise ValueError('invalid published constituent symbol')
        added=values.get('Date added')
        try:added=datetime.strptime(added,'%Y-%m-%d').date().isoformat()
        except (TypeError,ValueError):added=None
        members.append({'symbol':symbol,'name':values.get('Security'),'sector':values.get('GICS Sector'),
                        'industry':values.get('GICS Sub-Industry'),'membership_effective_at':added,
                        'effective_date_precision':'day' if added else 'unknown', 'source_row':values})
    if not 490<=len(members)<=520 or len({m['symbol'] for m in members})!=len(members):raise ValueError('published universe size or duplicate symbol invalid')
    return {'provider':'Wikipedia published S&P 500 constituent table','source_url':CONSTITUENTS_URL,
            'source_as_of':retrieved_at[:10],'retrieved_at':retrieved_at,'available_at':retrieved_at,
            'checksum':sha256_checksum(raw),'source_content':raw.decode('utf-8'),'source_content_rights':'Wikipedia CC BY-SA attribution retained via source URL','members':members,'scope':'published_current_SP500_constituents',
            'membership_effective_at':'per-row date_added where supplied; unknown otherwise',
            'historical_eligibility':'forward_collection_only',
            'limitations':['Published current list, not a licensed official index feed','Date added is source-supplied day precision; not proof of all historical removals or intraday membership changes']}

def rss_worker(task,*,fetcher=fetch):
    started=stamp();url='https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':task['query']+' when:2d','hl':'en-US','gl':'US','ceid':'US:en'})
    try:
        raw=fetcher(url);retrieved=stamp();root=ET.fromstring(raw);items=[]
        for row in root.findall('./channel/item')[:30]:
            published_raw=row.findtext('pubDate');published=published_raw
            try:published=parsedate_to_datetime(published).astimezone(UTC).isoformat().replace('+00:00','Z')
            except (TypeError,ValueError):published=None
            title=row.findtext('title') or ''
            link=row.findtext('link') or ''
            source=row.find('source')
            items.append({'news_id':'news_'+hashlib.sha256((link+'|'+title).encode()).hexdigest(),
                          'headline':title,'text':None,'text_status':'headline_only_no_article_body',
                          'url':link,'provider':'Google News RSS','source':source.text if source is not None else None,
                          'published_at_source_value':published_raw,'publication_time_transformation':'RFC2822 provider pubDate parsed to UTC ISO8601; no historical platform availability implied',
                          'symbols':[s for s in task['symbols'] if re.search(r'\b'+re.escape(s)+r'\b',title)],
                          'searched_symbols':task['symbols'],'published_at':published,'retrieved_at':retrieved,'available_at':retrieved,
                          'rights':'public_feed_headline_no_article_republication','quality':'publication_time_present' if published else 'quarantined_missing_publication_time',
                          'checksum':sha256_checksum(canonical_json_bytes({'headline':title,'url':link,'published_at':published}))})
        return {'worker_id':task['worker_id'],'worker_kind':'bounded_rss_retrieval','model':'none','status':'succeeded',
                'query':task['query'],'searched_symbols':task['symbols'],'started_at':started,'finished_at':retrieved,'url':url,
                'response_checksum':sha256_checksum(raw),'findings':items,'network_cost_usd':0,'cost_basis':'public_unpaid_HTTP_no_provider_charge'}
    except Exception as exc:
        return {'worker_id':task['worker_id'],'worker_kind':'bounded_rss_retrieval','model':'none','status':'failed','query':task['query'],
                'searched_symbols':task['symbols'],'started_at':started,'finished_at':stamp(),'url':url,'findings':[],'error':type(exc).__name__+': '+str(exc)}


def collect(output,*,fetcher=fetch):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    started=stamp();raw=fetcher(CONSTITUENTS_URL);u=constituent_universe(raw,stamp())
    (output/'universe-source.html').write_bytes(raw)
    proxy=None
    try:
        proxy_raw=fetcher(HOLDINGS_URL);proxy=universe(proxy_raw,stamp());(output/'universe-proxy-source.xlsx').write_bytes(proxy_raw)
    except Exception as exc:
        proxy={'status':'failed','error':type(exc).__name__+': '+str(exc)}
    symbols=sorted({r['symbol'] for r in u['members']})
    tasks=[{'worker_id':'macro','query':'financial markets Federal Reserve inflation economic outlook','symbols':[]},
           {'worker_id':'sectors','query':'S&P 500 technology healthcare financial energy industrial sectors','symbols':[]}]
    # Every symbol is assigned to a bounded query; absence of matching evidence remains visible.
    for n in range(0,len(symbols),20):
        batch=symbols[n:n+20]
        tasks.append({'worker_id':f'constituents-{n//20:02d}','query':' OR '.join('"'+s+' stock"' for s in batch),'symbols':batch})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        workers=list(pool.map(lambda t:rss_worker(t,fetcher=fetcher),tasks))
    items={}
    for worker in workers:
        for row in worker['findings']:
            if row['news_id'] in items:
                old=items[row['news_id']];old['searched_symbols']=sorted(set(old['searched_symbols']+row['searched_symbols']));old['symbols']=sorted(set(old['symbols']+row['symbols']))
            else:items[row['news_id']]=row
    coverage=[]
    for symbol in symbols:
        ws=[w for w in workers if symbol in w['searched_symbols']]
        matched=[r['news_id'] for r in items.values() if symbol in r['symbols'] and r['quality']!='quarantined_missing_publication_time']
        coverage.append({'symbol':symbol,'searched':True,'worker_ids':[w['worker_id'] for w in ws],
                         'status':'evidence' if matched else 'missing' if any(w['status']=='succeeded' for w in ws) else 'failed','news_ids':matched})
    result={'schema_version':VERSION,'started_at':started,'available_at':stamp(),'universe':u,'workers':workers,
            'news':sorted(items.values(),key=lambda r:r['news_id']),'coverage':coverage,
            'rights_policy':'public_titles_only; missing pubdate quarantined; no article body extraction',
            'research_scope':'markets_macro_sectors_full_dated_published_SP500_constituents','official_fund_holdings_crosscheck':proxy,
            'universe_discrepancy':{'only_published':sorted(set(symbols)-{r['symbol'] for r in (proxy or {}).get('members',[])}),
                                    'only_fund':sorted({r['symbol'] for r in (proxy or {}).get('members',[])}-set(symbols))},'cost':{'provider_usd':0,'basis':'public_unpaid_sources'},
            'limitations':['Published current constituent list is not licensed index-provider historical membership','batched search does not guarantee article coverage for every constituent']}
    (output/'research.json').write_bytes(canonical_json_bytes(result))
    return result

