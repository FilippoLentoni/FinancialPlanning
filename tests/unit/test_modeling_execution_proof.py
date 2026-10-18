"""A GSI omission cannot authorize a cleanup proof or retroactive paper execution."""
import copy
from datetime import datetime, timedelta

import pytest

from finplan_platform.core.errors import PlatformError
from finplan_platform.core.modeling_evidence import ModelingEvidence

AT = '2026-01-12T15:00:00Z'
NEXT = '2026-01-12T16:00:00Z'
LATER = '2026-01-12T17:00:00Z'


@pytest.fixture
def evidence(repo_any, artifacts):
    return ModelingEvidence(repo_any, artifacts)


def ready(e, slot, *, first=AT, started=None, status='succeeded', forecast=False):
    started = started or slot
    book = e.ensure_portfolio('strong-proof-test', 'rule', 1000., ['SPY'], AT)
    if e.read_registry('strong-proof-test') is None:
        e.register_approaches('strong-proof-test', [{'approach_id': 'rule', 'family': 'control'}], {'first_scheduled_slot': first, 'scheduled_start_at': first, 'cadence_seconds': 3600}, AT)
    shared = e.capture_shared_source('strong-proof-test', slot, {'availability_cutoff': started, 'input_snapshot_id': 'snap_01ARZ3NDEKTSV4RRFFQ69G5FAV', 'trigger': 'controlled_replay'}, started)
    supplied = e.capture_input('strong-proof-test', 'rule', slot, {'availability_cutoff': started, 'holdings': book, 'invocation_state': 'not_invoked' if status in ('failed', 'skipped', 'unavailable') else 'invoking', 'numeric': {'prepared': {'controlled_rule': 'half_cash'}}, 'news': {'consumed': False}}, shared, started)
    run = e.start_run('strong-proof-test', 'rule', slot, supplied['input_id'], book['portfolio_id'], started, {'family': 'control'})
    outcome = {'status': status, 'target_weights': {'SPY': .5}, 'cash_weight': .5}
    if forecast:
        outcome['forecast'] = {'available': True, 'expected_value': 1100., 'baseline_value': 1000., 'units': 'USD', 'target_at': '2026-02-12T15:00:00Z', 'forecast_checksum': 'sha256:controlled-forecast'}
    e.finish_run(run['run_id'], outcome, started)
    return book, supplied, run


def test_omitted_active_gsi_head_is_found_by_strong_expected_slot_read(evidence, monkeypatch):
    e = evidence
    ready(e, AT, status='failed')
    _, _, active = ready(e, NEXT)
    _, _, head = ready(e, LATER, status='unavailable')
    original = e.repo.query_index
    def omitted(table, index, partition, **kwargs):
        rows, token = original(table, index, partition, **kwargs)
        return ([r for r in rows if r.id != active['run_id']], token) if table == 'research_run' else (rows, token)
    monkeypatch.setattr(e.repo, 'query_index', omitted)
    with pytest.raises(PlatformError, match='another unresolved policy'):
        e.record_execution_frontier(head['run_id'], None, LATER)
    assert not e.read_run_header(head['run_id']).get('execution_frontier_ref')


def test_missing_expected_slot_blocks_proof_until_truthfully_accounted(evidence):
    e = evidence
    ready(e, AT, status='failed')
    _, _, head = ready(e, LATER, status='failed')
    with pytest.raises(PlatformError, match='missing a scheduled run'):
        e.record_execution_frontier(head['run_id'], None, LATER)
    assert not e.read_run_header(head['run_id']).get('execution_frontier_ref')
    ready(e, NEXT, started=LATER, status='skipped')
    proof = e.record_execution_frontier(head['run_id'], None, LATER)
    assert proof['strong_scheduled_slot_count'] == 3
    assert proof['schema_version'] == 'modeling-execution-frontier/2'


def test_manual_before_horizon_is_nontrading_and_forecast_leaves_pending_index(evidence):
    e = evidence
    book, supplied, manual = ready(e, AT, first=NEXT, forecast=True)
    before = e.read_input_bytes(supplied['input_id'])
    original = e.read_run(manual['run_id'])
    assert len(e.query_pending_forecasts('strong-proof-test', '2026-03-01T00:00:00Z')[0]) == 1
    with pytest.raises(PlatformError, match='outside the declared'):
        e.commit_paper_step(manual['run_id'], 1, book['paper_state'], [], {}, NEXT)
    disposition = e.mark_execution_ineligible(manual['run_id'], NEXT)
    assert disposition['status'] == 'excluded_nontrading' and disposition['reason_code'] == 'before_declared_comparison_horizon'
    assert disposition['eligible_from_slot'] == NEXT and disposition['ledger_committed'] is False and disposition['simulated_fills'] == []
    assert 'replacement_run_id' not in disposition
    assert e.mark_execution_ineligible(manual['run_id'], LATER) == disposition
    current = e.read_run(manual['run_id'])
    assert current['status'] == 'execution_excluded' and current['forecast_state'] == 'excluded_nontrading'
    assert current['output_ref'] == original['output_ref'] and current['outcome'] == original['outcome']
    assert e.read_input_bytes(supplied['input_id']) == before
    assert e.query_pending_forecasts('strong-proof-test', '2026-03-01T00:00:00Z')[0] == []
    assert e.get_portfolio(book['portfolio_id']) == book
    proof = e.record_execution_frontier(manual['run_id'], None, NEXT)
    assert proof['strong_scheduled_slot_count'] == 0
    _, _, eligible_head = ready(e, NEXT, first=NEXT, status='failed')
    next_proof = e.record_execution_frontier(eligible_head['run_id'], None, LATER, previous_frontier_run_id=manual['run_id'])
    assert next_proof['strong_scheduled_slot_count'] == 1


def test_late_created_old_policy_cannot_reenter_execution_after_frontier(evidence):
    e = evidence
    ready(e, AT, status='failed')
    book, _, head = ready(e, NEXT, status='failed')
    e.record_execution_frontier(head['run_id'], None, NEXT)
    # This unscheduled old half-hour head was genuinely created after the prior
    # frontier. Its actual creation time, rather than fabricated historical time,
    # permanently makes it nontrading under the declared hourly policy.
    _, _, late = ready(e, '2026-01-12T15:30:00Z', started=LATER)
    with pytest.raises(PlatformError, match='outside the declared'):
        e.commit_paper_step(late['run_id'], 1, book['paper_state'], [], {}, LATER)
    receipt = e.mark_execution_ineligible(late['run_id'], LATER)
    assert receipt['reason_code'] == 'created_after_decision_slot_closed'
    assert receipt['latest_started_at_for_slot'] == '2026-01-12T16:30:00Z'
    assert e.get_portfolio(book['portfolio_id']) == book
    events, _ = e.query_day('strong-proof-test', '2026-01-12', kind='observations')
    assert [r['kind'] for r in events] == ['execution_disposition']


def test_an_eligible_policy_cannot_be_arbitrarily_excluded(evidence):
    e = evidence
    book, _, run = ready(e, AT)
    with pytest.raises(PlatformError, match='not an available nontrading'):
        e.mark_execution_ineligible(run['run_id'], NEXT)
    assert e.read_run_header(run['run_id'])['status'] == 'succeeded'
    assert e.get_portfolio(book['portfolio_id']) == book


def test_strong_proof_has_bounded_deadline_independent_of_gsi_page_size(evidence):
    e = evidence
    ready(e, AT, status='failed')
    later = (datetime.fromisoformat(AT.replace('Z', '+00:00')) + timedelta(hours=168)).isoformat()
    _, _, head = ready(e, later, status='failed')
    with pytest.raises(PlatformError, match='168 scheduled slots'):
        e.record_execution_frontier(head['run_id'], None, later)
    assert not e.read_run_header(head['run_id']).get('execution_frontier_ref')


def test_new_registry_gsi_lag_uses_strongly_verified_committed_policy(evidence, monkeypatch):
    e = evidence
    original = e.repo.query_index
    def omit_registry(table, index, partition, **kwargs):
        return ([], None) if table == 'research_input' and partition.endswith('#__registry__') else original(table, index, partition, **kwargs)
    monkeypatch.setattr(e.repo, 'query_index', omit_registry)
    book, _, run = ready(e, AT)
    # The request mapping can change later; authoritative captured bytes cannot.
    cached = e._execution_policy_cache['strong-proof-test']
    assert cached['first_slot'] == AT
    result = e.commit_paper_step(run['run_id'], 1, book['paper_state'], [], {'reference_prices': {'SPY': 100.}, 'reference_date': '2026-01-12', 'transaction_cost_bps': 0.}, NEXT)
    assert result['execution_status'] == 'no_op'


def test_fresh_backend_registry_lag_is_retryable_blocker_not_false_exclusion(evidence, monkeypatch):
    e = evidence
    book, _, run = ready(e, AT)
    new_backend = ModelingEvidence(e.repo, e.store)
    original = e.repo.query_index
    def omit_registry(table, index, partition, **kwargs):
        return ([], None) if table == 'research_input' and partition.endswith('#__registry__') else original(table, index, partition, **kwargs)
    monkeypatch.setattr(e.repo, 'query_index', omit_registry)
    with pytest.raises(PlatformError) as error:
        new_backend.mark_execution_ineligible(run['run_id'], NEXT)
    assert error.value.code == 'DEPENDENCY_UNAVAILABLE' and error.value.retryable is True
    assert new_backend.get_portfolio(book['portfolio_id']) == book
    assert new_backend.read_run_header(run['run_id'])['status'] == 'succeeded'
    assert not new_backend.read_run_header(run['run_id']).get('execution_disposition_ref')
