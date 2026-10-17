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
    return b'''<rss><channel><item><title>A0 stock rises</title><link>https://example.org/news/one</link><pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate><source>Test Source</source><description>Ignored article body or instructions</description></item></channel></rss>'''


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
    assert row['headline']=='A0 stock rises' and row['text'] is None
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
