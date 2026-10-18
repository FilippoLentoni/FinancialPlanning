"""First-valid forecast matching uses a durable strong proof, never GSI absence."""
from datetime import datetime, timedelta, timezone

import pytest

from finplan_platform.core.errors import PlatformError
from finplan_platform.core.modeling_evidence import ModelingEvidence, _id
from tests.unit.test_modeling_reconciliation import ready, AT


@pytest.fixture
def evidence(repo_any, artifacts):
    return ModelingEvidence(repo_any, artifacts)


def stamp(hour):
    return (datetime(2026, 1, 12, 15, tzinfo=timezone.utc) + timedelta(hours=hour)).isoformat().replace('+00:00', 'Z')


def prepare(e, count=3, *, missing=None, running=None):
    saved = []
    for n in range(count):
        target = stamp(count - 1)
        book, item, run, _ = ready(e, stamp(n), status='running' if n == running else 'succeeded',
            outcome_extra={'forecast': {'available': True, 'status': 'pending', 'forecast_checksum': 'sha256:controlled-first-proof',
                'target_at': target, 'expected_value': 1000., 'baseline_value': 1000., 'units': 'USD'}} if n == 0 else None)
        mark = None
        if n != missing:
            at = stamp(n).replace(':00:00Z', ':05:00Z')
            mark = e.record_observation('reconciliation-test', 'rule', stamp(n), book['portfolio_id'], {
                'observed_at': at, 'value': 1000. + n, 'observation_kind': 'valid', 'quote_status': 'fresh',
                'run_id': run['run_id'], 'input_id': item['input_id']}, at)
        saved.append((book, item, run, mark))
    return saved


def test_ignores_entire_observation_gsi_and_selects_original_earliest(evidence, monkeypatch):
    e = evidence
    rows = prepare(e)
    original = e.repo.query_index
    monkeypatch.setattr(e.repo, 'query_index', lambda table, index, *a, **k: (_ for _ in ()).throw(AssertionError('observation GSI used')) if table == 'portfolio_observation' else original(table, index, *a, **k))
    proof = e.strong_forecast_observation(rows[0][2]['run_id'], stamp(3))
    assert proof['status'] == 'complete' and proof['observation']['observation_id'] == rows[2][3]['observation_id']
    assert proof['checked_slots'] == 3 and proof['proof_ref']
    assert e.get_portfolio(rows[0][0]['portfolio_id']) == rows[0][0]
    again = e.strong_forecast_observation(rows[0][2]['run_id'], stamp(4))
    assert again['status'] == 'complete' and again['checked_slots'] == 0 and again['proof_ref'] == proof['proof_ref']


@pytest.mark.parametrize('missing,running', [(1, None), (None, 1)])
def test_missing_or_running_earlier_slot_cannot_be_skipped(evidence, missing, running):
    rows = prepare(evidence, missing=missing, running=running)
    proof = evidence.strong_forecast_observation(rows[0][2]['run_id'], stamp(3))
    assert proof['status'] == 'pending_incomplete' and proof['observation'] is None
    assert proof['next_slot'] == stamp(1) and proof['checked_slots'] == 1
    assert evidence.read_run(rows[0][2]['run_id'])['forecast_state'] == 'pending'


def test_chunk_cursor_survives_restart_and_keeps_forecast_bytes(evidence):
    e = evidence
    rows = prepare(e, count=5)
    rid = rows[0][2]['run_id']
    original = e.read_run(rid)['output_ref']
    p1 = e.strong_forecast_observation(rid, stamp(5), max_slots=2)
    assert p1['status'] == 'pending_incomplete' and p1['next_slot'] == stamp(2)
    restarted = ModelingEvidence(e.repo, e.store)
    p2 = restarted.strong_forecast_observation(rid, stamp(5), max_slots=2)
    assert p2['status'] == 'pending_incomplete' and p2['next_slot'] == stamp(4)
    p3 = restarted.strong_forecast_observation(rid, stamp(5), max_slots=2)
    assert p3['status'] == 'complete' and p3['observation']['observation_id'] == rows[4][3]['observation_id']
    assert restarted.read_run(rid)['output_ref'] == original
    assert restarted.repo.require('research_run', rid).attrs['forecast_search_revision'] == 3


def test_late_old_slot_earlier_valid_mark_beats_newer_visible_mark(evidence):
    e = evidence
    rows = prepare(e, count=3, missing=1)
    rid = rows[0][2]['run_id']
    assert e.strong_forecast_observation(rid, stamp(3))['status'] == 'pending_incomplete'
    book, item, run, _ = rows[1]
    late = '2026-01-12T17:02:00Z'
    observed = e.record_observation('reconciliation-test', 'rule', stamp(1), book['portfolio_id'], {
        'observed_at': late, 'value': 1001., 'observation_kind': 'valid', 'quote_status': 'fresh',
        'run_id': run['run_id'], 'input_id': item['input_id']}, late)
    proof = e.strong_forecast_observation(rid, stamp(3))
    assert proof['status'] == 'complete' and proof['observation']['observation_id'] == observed['observation_id']


def test_future_observations_and_invalid_lower_time_bound_never_match(evidence):
    rows = prepare(evidence)
    result = evidence.strong_forecast_observation(rows[0][2]['run_id'], stamp(2))
    assert result['status'] != 'complete' and result['observation'] is None
    book = rows[0][0]
    with pytest.raises(PlatformError, match='not precede'):
        evidence.record_observation('reconciliation-test', 'rule', stamp(4), book['portfolio_id'], {
            'observed_at': AT, 'value': 1000., 'observation_kind': 'valid', 'quote_status': 'fresh'}, AT)


def test_mark_method_accepts_first_with_immutable_retry(evidence):
    e = evidence
    rows = prepare(e, count=4)
    rid = rows[0][2]['run_id']
    first = rows[3][3]
    value = {'forecast_checksum': 'sha256:controlled-first-proof', 'run_id': rid, 'portfolio_id': rows[0][0]['portfolio_id'],
        'observation_id': first['observation_id'], 'observed_at': '2026-01-12T18:05:00Z',
        'realized_value': 1003., 'signed_error': 3., 'absolute_error': 3., 'squared_error': 9.}
    receipt = e.record_observation('reconciliation-test', 'rule', AT, rows[0][0]['portfolio_id'], value, stamp(4), kind='forecast_match')
    result = e.mark_forecast_realized(rid, receipt['observation_id'], stamp(4))
    assert e.mark_forecast_realized(rid, receipt['observation_id'], stamp(5)) == result
    assert e.read_run(rid)['forecast_state'] == 'realized'


def test_future_mark_is_not_checkpointed_as_permanently_ineligible(evidence):
    rows = prepare(evidence)
    rid = rows[0][2]['run_id']
    early = evidence.strong_forecast_observation(rid, stamp(2))
    assert early['next_slot'] == stamp(2)
    mature = evidence.strong_forecast_observation(rid, stamp(3))
    assert mature['status'] == 'complete' and mature['observation']['observation_id'] == rows[2][3]['observation_id']


def test_concurrent_cursor_loser_never_selects_uncommitted_candidate(evidence, monkeypatch):
    from finplan_platform.core.repository import HeadMove
    e = evidence
    rows = prepare(e)
    rid = rows[0][2]['run_id']
    commit = e.repo.commit
    fired = []
    def race(ops, **kwargs):
        if not fired and any(isinstance(op, HeadMove) and op.revision_attr == 'forecast_search_revision' for op in ops):
            fired.append(True)
            assert e.strong_forecast_observation(rid, stamp(3))['status'] == 'complete'
        return commit(ops, **kwargs)
    monkeypatch.setattr(e.repo, 'commit', race)
    loser = e.strong_forecast_observation(rid, stamp(3))
    assert loser['status'] == 'pending_incomplete' and loser['observation'] is None
    assert e.strong_forecast_observation(rid, stamp(3))['status'] == 'complete'
    assert e.repo.require('research_run', rid).attrs['forecast_search_revision'] == 1
    assert e.get_portfolio(rows[0][0]['portfolio_id']) == rows[0][0]


def test_commit_rejects_valid_but_later_observation_despite_correct_errors(evidence):
    e = evidence
    rows = prepare(e, missing=1)
    book, item, run, _ = rows[1]
    at = '2026-01-12T17:02:00Z'
    earliest = e.record_observation('reconciliation-test', 'rule', stamp(1), book['portfolio_id'], {
        'observed_at': at, 'value': 1001., 'observation_kind': 'valid', 'quote_status': 'fresh',
        'run_id': run['run_id'], 'input_id': item['input_id']}, at)
    rid = rows[0][2]['run_id']
    before = e.read_run(rid)['output_ref']
    later = rows[2][3]
    wrong = {'forecast_checksum': 'sha256:controlled-first-proof', 'run_id': rid, 'portfolio_id': book['portfolio_id'],
        'observation_id': later['observation_id'], 'observed_at': '2026-01-12T17:05:00Z',
        'realized_value': 1002., 'signed_error': 2., 'absolute_error': 2., 'squared_error': 4.}
    receipt = e.record_observation('reconciliation-test', 'rule', AT, book['portfolio_id'], wrong, stamp(3), kind='forecast_match')
    with pytest.raises(PlatformError, match='first-valid strong'):
        e.mark_forecast_realized(rid, receipt['observation_id'], stamp(3))
    assert e.strong_forecast_observation(rid, stamp(3))['observation']['observation_id'] == earliest['observation_id']
    assert e.read_run(rid)['forecast_state'] == 'pending' and e.read_run(rid)['output_ref'] == before


def test_late_recording_time_preserves_original_observed_alignment(evidence):
    e = evidence
    rows = prepare(e, missing=2)
    book, item, run, _ = rows[2]
    at = '2026-01-12T17:05:00Z'
    saved = e.record_observation('reconciliation-test', 'rule', stamp(2), book['portfolio_id'], {
        'observed_at': at, 'value': 1002., 'observation_kind': 'valid', 'quote_status': 'fresh',
        'run_id': run['run_id'], 'input_id': item['input_id']}, at, recorded_at='2026-01-12T17:10:00Z')
    rid = rows[0][2]['run_id']
    early = e.strong_forecast_observation(rid, '2026-01-12T17:06:00Z')
    assert early['status'] == 'pending_incomplete' and early['next_slot'] == stamp(2)
    late = e.strong_forecast_observation(rid, '2026-01-12T17:11:00Z')
    assert late['status'] == 'complete' and late['observation']['observed_at'] == at
    assert late['observation']['observation_id'] == saved['observation_id']
    # A future-created proof also cannot be replayed into an earlier cutoff.
    assert e.strong_forecast_observation(rid, '2026-01-12T17:06:00Z')['status'] == 'pending_incomplete'


def test_future_terminal_receipt_cannot_prove_a_past_cutoff(evidence):
    e = evidence
    rows = prepare(e, running=2)
    e.finish_run(rows[2][2]['run_id'], {'status': 'succeeded', 'forecast': {'available': False}}, '2026-01-12T17:10:00Z')
    rid = rows[0][2]['run_id']
    assert e.strong_forecast_observation(rid, '2026-01-12T17:06:00Z')['status'] == 'pending_incomplete'
    assert e.strong_forecast_observation(rid, '2026-01-12T17:11:00Z')['status'] == 'complete'


def test_legacy_original_without_recorded_availability_remains_visible_pending(evidence):
    from finplan_platform.core.repository import PutNew
    e = evidence
    rows = prepare(e, missing=2)
    book, item, run, _ = rows[2]
    at = '2026-01-12T17:05:00Z'
    oid = _id('mo', 'reconciliation-test', 'rule', stamp(2), 'valuation')
    payload = {'observed_at': at, 'value': 1002., 'observation_kind': 'valid', 'quote_status': 'fresh',
        'run_id': run['run_id'], 'input_id': item['input_id']}
    ref = e._artifact('observations', oid, payload)
    legacy = {'schema_version': 'modeling-evidence/1', 'environment': 'beta', 'synthetic': True,
        'observation_id': oid, 'experiment_id': 'reconciliation-test', 'approach_id': 'rule', 'slot': stamp(2),
        'portfolio_id': book['portfolio_id'], 'portfolio_revision': 1, 'observed_at': at,
        'kind': 'valuation', **ref, **e._keys('reconciliation-test', 'rule', stamp(2), oid)}
    e.repo.commit([PutNew('portfolio_observation', doc=legacy)])
    result = e.strong_forecast_observation(rows[0][2]['run_id'], stamp(3))
    assert result['status'] == 'pending_incomplete' and result['reason'] == 'original_terminal_or_valuation_availability_unknown'
    assert e.repo.require('portfolio_observation', oid).doc == legacy
