"""Permanent action evidence survives proof retries/source rollover without ledger changes."""
import copy
import json

import pytest

from finplan_platform.core.errors import PlatformError
from finplan_platform.core.repository import HeadMove
from finplan_platform.core.modeling_evidence import ModelingEvidence

AT = '2026-01-12T15:00:00Z'
NEXT = '2026-01-12T16:00:00Z'
LATER = '2026-01-12T17:00:00Z'
RECORDED = '2026-01-12T17:05:00Z'


@pytest.fixture
def evidence(repo_any, artifacts):
    return ModelingEvidence(repo_any, artifacts)


def ready(e, slot=AT, *, experiment='reconciliation-test', approach='rule', status='succeeded', forecast=False, outcome_extra=None):
    book = e.ensure_portfolio(experiment, approach, 1000., ['SPY'], AT)
    if e.read_registry(experiment) is None:
        e.register_approaches(experiment, [{'approach_id': approach, 'family': 'control'}], {'first_scheduled_slot': AT, 'scheduled_start_at': AT, 'cadence_seconds': 3600}, AT)
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





def action(e, book, run, at=NEXT, **changes):
    payload = {'run_id': run['run_id'], 'observed_at': at, 'status': 'PENDING_ACTION_DATA', 'corporate_action_evidence': [{'instrument_id': 'SPY', 'event': {'dividend': .5, 'session_date': '2026-01-12'}, 'provider': 'CONTROLLED_TEST'}]}
    payload.update(changes)
    row = e.record_observation(book['experiment_id'], book['approach_id'], at, book['portfolio_id'], payload, at, kind='execution_attempt')
    return {'observation_id': row['observation_id'], 'checksum': row['checksum']}


def test_existing_proof_late_action_is_sticky_across_retry_new_policy_and_omitted_previous(evidence):
    e = evidence
    book, supplied, old, _ = ready(e)
    original_input = e.read_input_bytes(supplied['input_id'])
    original_output = e.read_artifact_bytes(e.read_run(old['run_id'])['output_ref'])
    original_doc = copy.deepcopy(e.repo.require('portfolio', book['portfolio_id']).doc)
    proof = e.record_execution_frontier(old['run_id'], old['run_id'], AT)
    proof_ref = e.read_run_header(old['run_id'])['execution_frontier_ref']
    original_proof = e.read_artifact_bytes(proof_ref)
    assert 'unresolved_action_ref' not in proof
    ref = action(e, book, old)
    merged = e.record_execution_frontier(old['run_id'], old['run_id'], NEXT, unresolved_action_ref=ref)
    assert merged['unresolved_action_ref'] == ref
    assert merged['unresolved_action_ref_source'] == 'authoritative_portfolio_action_head'
    assert e.record_execution_frontier(old['run_id'], old['run_id'], LATER)['unresolved_action_ref'] == ref
    assert e.read_artifact_bytes(proof_ref) == original_proof
    assert 'unresolved_action_ref' not in json.loads(original_proof)
    _, _, newer, _ = ready(e, NEXT)
    e.mark_execution_superseded(old['run_id'], newer['run_id'], LATER)
    # An omitted/stale caller frontier and an empty new source cannot clear the
    # independent strong book head. No original source is regenerated.
    newproof = e.record_execution_frontier(newer['run_id'], newer['run_id'], LATER)
    assert newproof['unresolved_action_ref'] == ref
    assert e.get_portfolio(book['portfolio_id'])['revision'] == book['revision']
    assert e.get_portfolio(book['portfolio_id'])['paper_state'] == book['paper_state']
    assert e.repo.require('portfolio', book['portfolio_id']).doc == original_doc
    assert 'unresolved_action_ref' not in e.repo.require('portfolio', book['portfolio_id']).view()
    assert e.read_input_bytes(supplied['input_id']) == original_input
    assert e.read_artifact_bytes(e.read_run(old['run_id'])['output_ref']) == original_output
    assert len([a for a in e.repo.audit_events(book['portfolio_id']) if a.operation == 'latch_unreconciled_paper_action']) == 1
    with pytest.raises(PlatformError, match='unresolved corporate action'):
        fill(e, book, newer)
    assert e.repo.get('portfolio_history', f"{book['portfolio_id']}:00000000000000000002") is None


def test_immediate_latch_survives_failed_cleanup_and_blocks_even_without_proof(evidence):
    e = evidence
    book, _, old, _ = ready(e)
    _, _, running, _ = ready(e, NEXT, status='running')
    ref = action(e, book, old)
    result = e.latch_action_blocker(old['run_id'], ref, NEXT)
    assert result['ref'] == ref
    with pytest.raises(PlatformError, match='running invocation'):
        e.record_execution_frontier(running['run_id'], old['run_id'], LATER)
    assert e.read_execution_frontier(old['run_id']) is None
    assert e.get_portfolio(book['portfolio_id'])['unresolved_action_ref'] == ref
    with pytest.raises(PlatformError, match='unresolved corporate action'):
        fill(e, book, old)


@pytest.mark.parametrize('bad', ['checksum', 'extra_path', 'wrong_kind', 'empty', 'status', 'foreign_run', 'future'])
def test_action_latch_requires_original_owned_available_evidence(evidence, bad):
    e = evidence
    book, _, old, _ = ready(e)
    kwargs = {}
    if bad == 'empty': kwargs['corporate_action_evidence'] = []
    if bad == 'status': kwargs['status'] = 'PENDING_MARKET_DATA'
    if bad == 'foreign_run': kwargs['run_id'] = ready(e, experiment='other')[2]['run_id']
    ref = action(e, book, old, **kwargs)
    if bad == 'checksum': ref['checksum'] = 'sha256:' + '0'*64
    if bad == 'extra_path': ref['artifact_key'] = 'modeling-evidence/v1/forged'
    if bad == 'wrong_kind':
        row = e.record_observation(book['experiment_id'], book['approach_id'], LATER, book['portfolio_id'], {'run_id': old['run_id'], 'status':'PENDING_ACTION_DATA', 'corporate_action_evidence':[{'dividend':1}]}, LATER, kind='not_execution_attempt')
        ref = {k:row[k] for k in ('observation_id','checksum')}
    with pytest.raises(PlatformError):
        e.latch_action_blocker(old['run_id'], ref, AT if bad == 'future' else LATER)
    assert 'unresolved_action_ref' not in e.get_portfolio(book['portfolio_id'])


def test_first_action_cannot_be_replaced_by_another_valid_receipt(evidence):
    e = evidence
    book, _, run, _ = ready(e)
    first = action(e, book, run)
    second = action(e, book, run, LATER)
    e.latch_action_blocker(run['run_id'], first, NEXT)
    assert e.latch_action_blocker(run['run_id'], first, LATER)['ref'] == first
    with pytest.raises(PlatformError, match='cannot be replaced'):
        e.record_execution_frontier(run['run_id'], run['run_id'], LATER, unresolved_action_ref=second)
    assert e.get_portfolio(book['portfolio_id'])['unresolved_action_ref'] == first


def test_concurrent_latch_prevents_ledger_commit_in_transaction(evidence, monkeypatch):
    e = evidence
    book, _, run, _ = ready(e)
    ref = action(e, book, run)
    original = e.repo.commit
    fired = False
    def race(ops, **kwargs):
        nonlocal fired
        if not fired and any(isinstance(op, HeadMove) and op.revision_attr == 'paper_state_revision' for op in ops):
            fired = True
            e.latch_action_blocker(run['run_id'], ref, NEXT)
        return original(ops, **kwargs)
    monkeypatch.setattr(e.repo, 'commit', race)
    with pytest.raises(PlatformError):
        fill(e, book, run)
    assert fired and e.get_portfolio(book['portfolio_id'])['revision'] == 1
    assert not e.read_run_header(run['run_id']).get('execution_ref')
    assert e.repo.get('portfolio_history', f"{book['portfolio_id']}:00000000000000000002") is None
