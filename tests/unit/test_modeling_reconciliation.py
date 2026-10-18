"""Metadata-only selection and permanent nonledger dispositions, fake + Dynamo syntax."""
import copy

import pytest

from finplan_platform.core.errors import PlatformError
from finplan_platform.core.modeling_evidence import ModelingEvidence
from finplan_platform.core.repository import Transition

AT = '2026-01-12T15:00:00Z'
NEXT = '2026-01-12T16:00:00Z'
LATER = '2026-01-12T17:00:00Z'
RECORDED = '2026-01-12T17:05:00Z'


@pytest.fixture
def evidence(repo_any, artifacts):
    return ModelingEvidence(repo_any, artifacts)


def ready(e, slot=AT, *, experiment='reconciliation-test', approach='rule', status='succeeded', forecast=False, outcome_extra=None):
    book = e.ensure_portfolio(experiment, approach, 1000., ['SPY'], AT)
    source = e.capture_shared_source(experiment, slot, {'availability_cutoff': slot, 'input_snapshot_id': 'snap_01ARZ3NDEKTSV4RRFFQ69G5FAV'}, slot)
    item = e.capture_input(experiment, approach, slot, {'availability_cutoff': slot, 'holdings': book, 'news': {'consumed': False}, 'numeric': {'prepared': {'rule': 'controlled fixture'}}}, source, slot)
    run = e.start_run(experiment, approach, slot, item['input_id'], book['portfolio_id'], slot, {'family': 'control'})
    outcome = {'status': status, 'target_weights': {'SPY': .5}, 'cash_weight': .5}
    if forecast:
        outcome['forecast'] = {'available': True, 'status': 'pending', 'expected_value': 1100., 'baseline_value': 1000., 'units': 'USD', 'target_at': '2026-02-12T15:00:00Z', 'forecast_checksum': 'sha256:controlled-forecast'}
    outcome.update(outcome_extra or {})
    if status != 'running':
        e.finish_run(run['run_id'], outcome, slot)
    return book, item, run, outcome


def fill(e, book, run):
    state = {**book['paper_state'], 'positions': [{'instrument_id': 'SPY', 'quantity': 5}], 'cash_balance': 499.5, 'source': 'paper_rebalance'}
    execution = {'market_session_valid': True, 'evaluator_version': 'CONTROLLED_REPLAY', 'quote_provenance': {'source': 'CONTROLLED_REPLAY'}, 'execution_at': RECORDED, 'reference_prices': {'SPY': 100}, 'transaction_cost_bps': 10, 'before_value': 1000, 'after_value': 999.5, 'transaction_cost': .5}
    fills = [{'instrument_id': 'SPY', 'quantity': 5, 'price': 100, 'cost': .5}]
    return e.commit_paper_step(run['run_id'], 1, state, fills, execution, RECORDED)


def test_header_query_is_bounded_newest_first_and_never_loads_artifacts_or_gets(evidence, monkeypatch):
    e = evidence
    records = [ready(e, slot)[2] for slot in (AT, NEXT, LATER)]
    e.claim_run(records[-1]['run_id'], 'not-applicable-terminal', LATER)
    def forbidden(*args, **kwargs):
        raise AssertionError('Header query performed artifact or individual GET expansion')
    monkeypatch.setattr(e.store, 'get', forbidden)
    monkeypatch.setattr(e.repo, 'get', forbidden)
    rows, token = e.query_run_headers('reconciliation-test', 'rule', LATER, page_size=1)
    assert [r['run_id'] for r in rows] == [records[1]['run_id']] and token
    rows, token = e.query_run_headers('reconciliation-test', 'rule', '2026-01-12T12:00:00-05:00', page_size=1, next_token=token)
    assert [r['run_id'] for r in rows] == [records[0]['run_id']] and token is None
    for size in (0, 101):
        with pytest.raises(PlatformError):
            e.query_run_headers('reconciliation-test', 'rule', LATER, page_size=size)


def test_header_cursor_cannot_change_experiment_approach_cutoff_or_table(evidence):
    e = evidence
    ready(e, AT); ready(e, NEXT)
    _, token = e.query_run_headers('reconciliation-test', 'rule', LATER, page_size=1)
    for experiment, approach, cutoff in [('other', 'rule', LATER), ('reconciliation-test', 'other', LATER), ('reconciliation-test', 'rule', NEXT)]:
        with pytest.raises(PlatformError, match='wrong query scope'):
            e.query_run_headers(experiment, approach, cutoff, next_token=token)
    _, wrong = e.query_day('reconciliation-test', '2026-01-12', kind='inputs', page_size=1)
    with pytest.raises(PlatformError, match='wrong query scope'):
        e.query_run_headers('reconciliation-test', 'rule', LATER, next_token=wrong)


def test_strong_header_exposes_lease_and_does_not_load_original_input(evidence, monkeypatch):
    e = evidence
    _, _, run, _ = ready(e, status='running')
    assert e.claim_run(run['run_id'], 'worker-one', AT, lease_seconds=60)
    monkeypatch.setattr(e.store, 'get', lambda *a, **k: pytest.fail('Strong metadata lookup fetched S3'))
    header = e.read_run_header(run['run_id'])
    assert header['status'] == 'running' and header['lease_owner'] == 'worker-one'
    assert header['lease_until'] > 0 and header['invocation_revision'] == 1
    rows, _ = e.query_run_headers('reconciliation-test', 'rule', NEXT)
    assert rows[0]['lease_until'] == header['lease_until']


def test_disposition_is_permanent_audited_nonledger_and_preserves_forecast(evidence):
    e = evidence
    book, item, old, outcome = ready(e, forecast=True)
    _, _, newer, _ = ready(e, NEXT)
    _, _, newest, _ = ready(e, LATER)
    original_input = e.read_input_bytes(item['input_id'])
    before = e.read_run(old['run_id'])
    old_output = e.read_artifact_bytes(before['output_ref'])
    history = copy.deepcopy(e.repo.require('portfolio_history', f"{book['portfolio_id']}:00000000000000000001").doc)
    receipt = e.mark_execution_superseded(old['run_id'], newer['run_id'], RECORDED)
    assert receipt['status'] == 'superseded' and receipt['ledger_committed'] is False and receipt['simulated_fills'] == []
    assert receipt['replacement_run_id'] == newer['run_id']
    assert e.mark_execution_superseded(old['run_id'], newest['run_id'], '2026-01-13T15:00:00Z') == receipt
    reread = e.read_run(old['run_id'])
    assert reread['status'] == 'superseded' and reread['outcome'] == outcome
    assert reread['execution_disposition'] == receipt and reread['execution_disposition_ref']
    assert not reread.get('execution_ref') and reread['execution'] is None
    assert e.read_input_bytes(item['input_id']) == original_input
    assert e.read_artifact_bytes(reread['output_ref']) == old_output
    assert reread['forecast_state'] == 'pending' and reread['forecast_target'] == before['forecast_target']
    assert e.get_portfolio(book['portfolio_id']) == book
    assert e.repo.require('portfolio_history', f"{book['portfolio_id']}:00000000000000000001").doc == history
    assert e.repo.get('portfolio_history', f"{book['portfolio_id']}:00000000000000000002") is None
    events, _ = e.query_day('reconciliation-test', '2026-01-12', kind='observations')
    assert len(events) == 1 and events[0]['kind'] == 'execution_disposition' and events[0]['payload'] == receipt
    assert len([a for a in e.repo.audit_events(old['run_id']) if a.operation == 'supersede_pending_experiment_policy']) == 1
    with pytest.raises(PlatformError, match='no applicable recommendation'):
        fill(e, book, old)


@pytest.mark.parametrize('kind', ['same_slot', 'other_approach', 'other_experiment', 'running', 'failed', 'future', 'invalid_allocation'])
def test_disposition_rejects_ineligible_replacement_binding(evidence, kind):
    e = evidence
    book, _, old, _ = ready(e)
    kwargs = {'approach': 'another'} if kind == 'other_approach' else {'experiment': 'another'} if kind == 'other_experiment' else {'status': kind} if kind in ('running', 'failed') else {'outcome_extra': {'target_weights': {'SPY': 2.}, 'cash_weight': -1.}} if kind == 'invalid_allocation' else {}
    replacement = old if kind == 'same_slot' else ready(e, NEXT, **kwargs)[2]
    at = AT if kind == 'future' else RECORDED
    with pytest.raises(PlatformError):
        e.mark_execution_superseded(old['run_id'], replacement['run_id'], at)
    assert e.read_run_header(old['run_id'])['status'] == 'succeeded'
    assert e.get_portfolio(book['portfolio_id']) == book
    assert e.query_day('reconciliation-test', '2026-01-12', kind='observations')[0] == []


def test_fill_wins_race_disposition_transaction_creates_no_event(evidence, monkeypatch):
    e = evidence
    book, _, old, _ = ready(e)
    _, _, replacement, _ = ready(e, NEXT)
    commit = e.repo.commit
    fired = []
    def race(ops, **kwargs):
        if not fired and any(isinstance(op, Transition) and op.record_id == old['run_id'] and op.to_status == 'superseded' for op in ops):
            fired.append(True); fill(e, book, old)
        return commit(ops, **kwargs)
    monkeypatch.setattr(e.repo, 'commit', race)
    with pytest.raises(PlatformError):
        e.mark_execution_superseded(old['run_id'], replacement['run_id'], RECORDED)
    assert e.read_run_header(old['run_id'])['status'] == 'applied'
    assert not e.read_run_header(old['run_id']).get('execution_disposition_ref')
    assert e.get_portfolio(book['portfolio_id'])['revision'] == 2
    events, _ = e.query_day('reconciliation-test', '2026-01-12', kind='observations')
    assert [r['kind'] for r in events] == ['paper_execution']


def test_disposition_wins_race_fill_transaction_cannot_move_ledger(evidence, monkeypatch):
    e = evidence
    book, _, old, _ = ready(e)
    _, _, replacement, _ = ready(e, NEXT)
    commit = e.repo.commit
    fired = []
    def race(ops, **kwargs):
        if not fired and any(isinstance(op, Transition) and op.record_id == old['run_id'] and op.to_status == 'applied' for op in ops):
            fired.append(True); e.mark_execution_superseded(old['run_id'], replacement['run_id'], RECORDED)
        return commit(ops, **kwargs)
    monkeypatch.setattr(e.repo, 'commit', race)
    with pytest.raises(PlatformError):
        fill(e, book, old)
    assert e.read_run_header(old['run_id'])['status'] == 'superseded'
    assert not e.read_run_header(old['run_id']).get('execution_ref')
    assert e.get_portfolio(book['portfolio_id']) == book
    events, _ = e.query_day('reconciliation-test', '2026-01-12', kind='observations')
    assert [r['kind'] for r in events] == ['execution_disposition']


def test_concurrent_disposition_reuses_winning_original_receipt(evidence, monkeypatch):
    e = evidence
    _, _, old, _ = ready(e)
    _, _, replacement, _ = ready(e, NEXT)
    _, _, latest, _ = ready(e, LATER)
    commit = e.repo.commit
    fired = []
    def race(ops, **kwargs):
        if not fired and any(isinstance(op, Transition) and op.record_id == old['run_id'] and op.to_status == 'superseded' for op in ops):
            fired.append(True); e.mark_execution_superseded(old['run_id'], latest['run_id'], RECORDED)
        return commit(ops, **kwargs)
    monkeypatch.setattr(e.repo, 'commit', race)
    receipt = e.mark_execution_superseded(old['run_id'], replacement['run_id'], RECORDED)
    assert receipt['replacement_run_id'] == latest['run_id']
    assert e.query_day('reconciliation-test', '2026-01-12', kind='observations')[0][0]['payload'] == receipt
    assert len([a for a in e.repo.audit_events(old['run_id']) if a.operation == 'supersede_pending_experiment_policy']) == 1


def test_frontier_rejects_incomplete_older_cleanup_and_retains_one_pending_policy(evidence):
    e = evidence
    book, _, oldest, _ = ready(e)
    _, _, pending, _ = ready(e, NEXT)
    _, _, head, _ = ready(e, LATER, status='unavailable')
    with pytest.raises(PlatformError, match='another unresolved policy'):
        e.record_execution_frontier(head['run_id'], pending['run_id'], RECORDED)
    assert not e.read_run_header(head['run_id']).get('execution_frontier_ref')
    e.mark_execution_superseded(oldest['run_id'], pending['run_id'], RECORDED)
    proof = e.record_execution_frontier(head['run_id'], pending['run_id'], RECORDED)
    assert proof['pending_run_id'] == pending['run_id'] and proof['covered_through_slot'] == LATER
    assert e.read_execution_frontier(head['run_id']) == proof
    assert e.read_run_header(head['run_id'])['execution_frontier_revision'] == 1
    assert e.record_execution_frontier(head['run_id'], None, '2026-01-12T18:05:00Z') == proof
    assert e.get_portfolio(book['portfolio_id']) == book
    assert len([a for a in e.repo.audit_events(head['run_id']) if a.operation == 'checkpoint_execution_cleanup']) == 1


def test_failed_streak_frontier_keeps_old_pending_pointer_without_fetching_model_arrays(evidence, monkeypatch):
    e = evidence
    book, _, pending, _ = ready(e)
    _, _, first, _ = ready(e, NEXT, status='failed')
    first_proof = e.record_execution_frontier(first['run_id'], pending['run_id'], RECORDED)
    _, _, second, _ = ready(e, LATER, status='unavailable')
    original_get = e.store.get
    original_query = e.repo.query_index
    bounds = []
    def small_only(role, key, **kwargs):
        assert '/execution-frontiers/' in key, 'Cleanup fetched an original model/input/output artifact'
        return original_get(role, key, **kwargs)
    def query(*args, **kwargs):
        bounds.append(kwargs.get('sort_lower_bound'))
        return original_query(*args, **kwargs)
    monkeypatch.setattr(e.store, 'get', small_only)
    monkeypatch.setattr(e.repo, 'query_index', query)
    proof = e.record_execution_frontier(second['run_id'], pending['run_id'], RECORDED, previous_frontier_run_id=first['run_id'])
    assert proof['pending_run_id'] == first_proof['pending_run_id'] == pending['run_id']
    assert proof['previous_frontier_ref']['frontier_run_id'] == first['run_id']
    assert bounds == [first['time_key']]
    assert e.get_portfolio(book['portfolio_id']) == book
    monkeypatch.setattr(e.store, 'get', original_get)
    _, _, third, _ = ready(e, '2026-01-12T18:00:00Z', status='failed')
    with pytest.raises(PlatformError, match='another unresolved policy'):
        e.record_execution_frontier(third['run_id'], None, '2026-01-12T18:05:00Z', previous_frontier_run_id=second['run_id'])
    assert not e.read_run_header(third['run_id']).get('execution_frontier_ref')
    fill(e, book, pending)
    resolved = e.record_execution_frontier(third['run_id'], None, '2026-01-12T18:05:00Z', previous_frontier_run_id=second['run_id'])
    assert resolved['pending_run_id'] is None
    assert next(h for h in resolved['checked_headers'] if h['run_id'] == pending['run_id'])['status'] == 'applied'


def test_frontier_for_always_unavailable_approach_queries_only_new_interval(evidence, monkeypatch):
    e = evidence
    _, _, first, _ = ready(e, AT, status='unavailable')
    e.record_execution_frontier(first['run_id'], None, RECORDED)
    _, _, second, _ = ready(e, NEXT, status='unavailable')
    queries = []
    original = e.repo.query_index
    def query(*args, **kwargs):
        queries.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(e.repo, 'query_index', query)
    proof = e.record_execution_frontier(second['run_id'], None, RECORDED, previous_frontier_run_id=first['run_id'])
    assert len(queries) == 1 and queries[0]['sort_lower_bound'] == first['time_key']
    assert proof['pending_run_id'] is None and {h['run_id'] for h in proof['checked_headers']} == {second['run_id']}


def test_running_invocation_blocks_frontier_even_after_lease_expiry_until_reaped(evidence):
    e = evidence
    _, item, old, _ = ready(e, status='running')
    original = e.read_input_bytes(item['input_id'])
    assert e.claim_run(old['run_id'], 'crashed-worker', AT, lease_seconds=60)
    _, _, newest, _ = ready(e, NEXT, status='unavailable')
    with pytest.raises(PlatformError, match='running invocation'):
        e.record_execution_frontier(newest['run_id'], None, RECORDED)
    assert not e.read_run_header(newest['run_id']).get('execution_frontier_ref')
    e.finish_run(old['run_id'], {'status': 'failed', 'reason': 'controlled recovered interruption; no inference repeated'}, RECORDED)
    assert e.read_input_bytes(item['input_id']) == original
    proof = e.record_execution_frontier(newest['run_id'], None, RECORDED)
    assert proof['pending_run_id'] is None


def test_frontier_rejects_cross_book_chain_future_head_and_invalid_pending(evidence):
    e = evidence
    _, _, first, _ = ready(e, status='failed')
    e.record_execution_frontier(first['run_id'], None, RECORDED)
    _, _, other, _ = ready(e, NEXT, approach='other', status='failed')
    e.record_execution_frontier(other['run_id'], None, RECORDED)
    _, _, head, _ = ready(e, LATER, status='failed')
    for kwargs in ({'previous_frontier_run_id': other['run_id']}, {'pending_run_id': first['run_id']}, {'previous_frontier_run_id': head['run_id']}):
        arguments = {'pending_run_id': None, **kwargs}
        with pytest.raises(PlatformError):
            e.record_execution_frontier(head['run_id'], recorded_at=RECORDED, **arguments)
    with pytest.raises(PlatformError, match='not yet available'):
        e.record_execution_frontier(head['run_id'], None, AT)
    assert not e.read_run_header(head['run_id']).get('execution_frontier_ref')


def test_frontier_metadata_overflow_never_advances_checkpoint(evidence, monkeypatch):
    e = evidence
    _, _, head, _ = ready(e, status='failed')
    pages = []
    def overflowing(*args, **kwargs):
        pages.append(1)
        return [], 'bounded-test-cursor'
    monkeypatch.setattr(e.repo, 'query_index', overflowing)
    with pytest.raises(PlatformError, match='10000 rows'):
        e.record_execution_frontier(head['run_id'], None, RECORDED)
    assert len(pages) == 100 and not e.read_run_header(head['run_id']).get('execution_frontier_ref')


def test_corrupt_prior_frontier_blocks_advance_without_model_or_ledger_changes(evidence, monkeypatch):
    e = evidence
    book, _, first, _ = ready(e, status='failed')
    e.record_execution_frontier(first['run_id'], None, RECORDED)
    _, _, head, _ = ready(e, NEXT, status='failed')
    original = e.store.get
    prior_key = e.read_run_header(first['run_id'])['execution_frontier_ref']['artifact_key']
    def corrupt(role, key, **kwargs):
        if key == prior_key:
            raise PlatformError.validation('controlled corrupt frontier checksum')
        return original(role, key, **kwargs)
    monkeypatch.setattr(e.store, 'get', corrupt)
    with pytest.raises(PlatformError, match='corrupt frontier'):
        e.record_execution_frontier(head['run_id'], None, RECORDED, previous_frontier_run_id=first['run_id'])
    assert e.get_portfolio(book['portfolio_id']) == book and not e.read_run_header(head['run_id']).get('execution_frontier_ref')
