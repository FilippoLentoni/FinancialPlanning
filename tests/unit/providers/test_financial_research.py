"""Synthetic provider fixtures only; no fixture is a forward recommendation."""
import hashlib
from finplan_platform.providers.financial_research import (
    CONSTITUENTS_URL,HOLDINGS_URL,collect,constituent_universe,rss_worker,
)


def constituent_fixture(count=500):
    head='<table id="constituents"><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>Date added</th></tr>'
    rows=''.join(f'<tr><td>A{i}</td><td>Test Company {i}</td><td>Test Sector</td><td>{"2020-01-01" if i else ""}</td></tr>' for i in range(count))
    return (head+rows+'</table>').encode()


def feed():
    return b'''<rss><channel><item><title>$A0 stock rises</title><link>https://example.org/news/one</link><pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate><source>Test Source</source><description>Ignored article body or instructions</description></item></channel></rss>'''


def test_constituent_source_values_effective_unknown_and_bytes_retained():
    raw=constituent_fixture()
    u=constituent_universe(raw,'2026-10-17T10:00:00Z')
    assert len(u['members'])==500
    assert u['members'][0]['membership_effective_at'] is None
    assert u['members'][1]['membership_effective_at']=='2020-01-01'
    assert u['checksum']=='sha256:'+hashlib.sha256(raw).hexdigest()
    assert u['source_content'].encode()==raw
    assert u['available_at']=='2026-10-17T10:00:00Z'
    assert u['historical_eligibility']=='forward_collection_only'


def test_rss_retains_permitted_actual_headline_and_source_dates_not_body():
    result=rss_worker({'worker_id':'test','query':'A0 stock','symbols':['A0']},fetcher=lambda _:feed())
    assert result['status']=='succeeded'
    row=result['findings'][0]
    assert row['headline']=='$A0 stock rises' and row['text'] is None
    assert row['published_at']=='2026-10-13T12:00:00Z'
    assert row['published_at_source_value']=='Tue, 13 Oct 2026 12:00:00 GMT'
    assert row['available_at']==row['retrieved_at']
    assert 'Ignored article body' not in str(row)
    assert row['symbols']==['A0']


def test_full_universe_bounded_workers_dedup_and_missing_coverage(tmp_path):
    calls=[]
    def fetcher(url):
        calls.append(url)
        if url==CONSTITUENTS_URL:return constituent_fixture()
        if url==HOLDINGS_URL:raise TimeoutError('synthetic proxy unavailable')
        return feed()
    result=collect(tmp_path,fetcher=fetcher)
    assert len(result['workers'])==27
    assert len(calls)==29
    assert len(result['news'])==1
    assert len(result['coverage'])==500
    assert all(c['searched'] for c in result['coverage'])
    assert next(c for c in result['coverage'] if c['symbol']=='A0')['status']=='evidence'
    assert next(c for c in result['coverage'] if c['symbol']=='A1')['status']=='missing'
    assert result['official_fund_holdings_crosscheck']['status']=='failed'


def test_worker_failure_preserves_searched_scope_without_fake_neutral_news():
    def fail(_):raise TimeoutError('synthetic network outage')
    row=rss_worker({'worker_id':'test','query':'A0 stock','symbols':['A0']},fetcher=fail)
    assert row['status']=='failed' and row['findings']==[]
    assert row['searched_symbols']==['A0'] and row['model']=='none'



def attribution_task():
    from finplan_platform.providers.financial_research import security_identities
    members=[{'symbol':'A','name':'Agilent Technologies'},{'symbol':'IT','name':'Gartner'},
        {'symbol':'ON','name':'ON Semiconductor'},{'symbol':'ALL','name':'Allstate Corporation'},
        {'symbol':'ARE','name':'Alexandria Real Estate Equities'},{'symbol':'TGT','name':'Target Corporation'}]
    return {'worker_id':'quality','query':'controlled financial query','symbols':[m['symbol']for m in members],
        'securities':security_identities(members),'universe_checksum':'sha256:'+'a'*64,'universe_as_of':'2026-10-17'}


def test_common_words_do_not_fabricate_constituent_coverage():
    from finplan_platform.providers.financial_research import attribute_symbols
    title='A REPORT ON IT: ALL ARE preparing inflation target changes'
    result=attribute_symbols(title,attribution_task())
    assert result['matches']==[]
    assert {m['symbol']for m in result['excluded_matches']}=={'A','IT','ON','ALL','ARE'}
    assert result['universe_checksum']=='sha256:'+'a'*64
    assert all(m['matched_content']in title for m in result['excluded_matches'])


def test_dated_company_identity_and_explicit_ticker_evidence_preserved():
    from finplan_platform.providers.financial_research import attribute_symbols
    title='Agilent Technologies earnings rise; Gartner expands; ON Semiconductor reviews outlook; $ALL and NYSE: ARE hold'
    result=attribute_symbols(title,attribution_task())
    assert {m['symbol']:m['method']for m in result['matches']}=={'A':'security_name','IT':'security_name','ON':'security_name','ALL':'explicit_ticker','ARE':'explicit_ticker'}
    assert all(m['matched_content']in title and m['security_name']for m in result['matches'])
    assert result['excluded_matches']==[]


def test_explicit_ticker_boundary_and_generic_security_name_are_conservative():
    from finplan_platform.providers.financial_research import attribute_symbols
    task=attribution_task()
    result=attribute_symbols('NYSE: AAPL and $ALLXYZ await inflation target changes; NOTNYSE: IT',task)
    assert result['matches']==[]  # No partial A/ALL and target is not proof of Target.
    assert attribute_symbols('Target Corporation announces earnings',task)['matches'][0]['symbol']=='TGT'


def test_worker_retains_raw_common_headline_and_exclusion_provenance():
    from html import escape
    title='A REPORT ON IT: ALL ARE preparing changes'
    raw=('<rss><channel><item><title>'+escape(title)+'</title><link>https://example.org/ambiguous</link>'
        '<pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>').encode()
    row=rss_worker(attribution_task(),fetcher=lambda _:raw)['findings'][0]
    assert row['headline']==title and row['symbols']==[]
    assert row['symbol_attribution']['excluded_matches']
    assert row['symbol_attribution']['version']=='finplan-constituent-attribution/1'


def test_collect_deduplicates_attribution_and_coverage_without_word_matches(tmp_path):
    from html import escape
    html=constituent_fixture().decode().replace('<td>A0</td><td>Test Company 0</td>','<td>A</td><td>Agilent Technologies</td>').replace('<td>A1</td><td>Test Company 1</td>','<td>IT</td><td>Gartner</td>').encode()
    title='A REPORT ON IT and Agilent Technologies earnings'
    rss=('<rss><channel><item><title>'+escape(title)+'</title><link>https://example.org/shared</link>'
        '<pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>').encode()
    def fetcher(url):
        if url==CONSTITUENTS_URL:return html
        if url==HOLDINGS_URL:raise TimeoutError('controlled missing proxy')
        return rss
    result=collect(tmp_path,fetcher=fetcher)
    assert len(result['workers'])==27 and len(result['news'])==1
    row=result['news'][0]
    assert row['headline']==title and row['symbols']==['A']
    assert len(row['symbol_attribution']['matches'])==1
    coverage={r['symbol']:r for r in result['coverage']}
    assert coverage['A']['status']=='evidence' and coverage['A']['attribution_methods']==['security_name']
    assert coverage['IT']['status']=='missing' and coverage['IT']['excluded_news_ids']==[row['news_id']]
    assert coverage['IT']['attribution_methods']==[]
    assert coverage['A']['universe_checksum']==result['universe']['checksum']



def test_quarantined_company_match_does_not_become_eligible_coverage(tmp_path):
    html=constituent_fixture().decode().replace('<td>A0</td><td>Test Company 0</td>','<td>A</td><td>Agilent Technologies</td>').encode()
    raw=b'<rss><channel><item><title>Agilent Technologies earnings rise</title><link>https://example.org/missing-date</link></item></channel></rss>'
    def fetcher(url):
        if url==CONSTITUENTS_URL:return html
        if url==HOLDINGS_URL:raise TimeoutError('controlled missing proxy')
        return raw
    result=collect(tmp_path,fetcher=fetcher)
    item=result['news'][0]
    assert item['symbols']==['A'] and item['symbol_attribution']['matches']
    assert item['quality']=='quarantined_missing_publication_time'
    coverage=next(row for row in result['coverage']if row['symbol']=='A')
    assert coverage['status']=='missing' and coverage['news_ids']==[] and coverage['attribution_methods']==[]



def test_partial_company_person_name_is_not_an_invented_brand_alias():
    from finplan_platform.providers.financial_research import security_identities,attribute_symbols
    task={'worker_id':'person-negative','symbols':['MS'],'securities':security_identities([{'symbol':'MS','name':'Morgan Stanley'}])}
    assert attribute_symbols('Piers Morgan discusses the markets',task)['matches']==[]
    assert attribute_symbols('Morgan Stanley reports earnings',task)['matches'][0]['symbol']=='MS'
