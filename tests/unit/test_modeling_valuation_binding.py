"""New valuation metadata supports safe retries; legacy evidence remains immutable."""
import copy

import pytest

from finplan_platform.core.errors import PlatformError
from finplan_platform.core.modeling_evidence import ModelingEvidence, _id
from finplan_platform.core.repository import PutNew

AT = '2026-01-12T15:00:00Z'


@pytest.fixture
def evidence(repo_any, artifacts):
    return ModelingEvidence(repo_any, artifacts)


def ready(e, approach='rule'):
    book = e.ensure_portfolio('valuation-test', approach, 1000., ['SPY'], AT)
    supplied = e.capture_input('valuation-test', approach, AT, {'availability_cutoff': AT, 'numeric': {'prepared': {'rule': 'cash'}}}, None, AT)
    run = e.start_run('valuation-test', approach, AT, supplied['input_id'], book['portfolio_id'], AT)
    payload = {'run_id': run['run_id'], 'input_id': supplied['input_id'], 'value': 1000., 'revision': 1, 'observation_kind': 'valid', 'quote_status': 'fresh', 'observed_at': AT}
    return book, run, payload


def test_new_valuation_metadata_has_verified_original_run_and_input_binding(evidence):
    e = evidence
    book, run, payload = ready(e)
    record = e.record_observation('valuation-test', 'rule', AT, book['portfolio_id'], payload, AT)
    assert record['run_id'] == run['run_id'] and record['input_id'] == run['input_id']
    assert e.repo.require('portfolio_observation', record['observation_id']).doc == record
    assert e.record_observation('valuation-test', 'rule', AT, book['portfolio_id'], payload, AT) == record


@pytest.mark.parametrize('defect', ['wrong_run', 'wrong_input', 'missing_input'])
def test_wrong_bound_valuation_cannot_be_published(evidence, defect):
    e = evidence
    book, run, payload = ready(e)
    _, other, _ = ready(e, approach='other')
    if defect == 'wrong_run':
        payload['run_id'] = other['run_id']
    elif defect == 'wrong_input':
        payload['input_id'] = other['input_id']
    else:
        payload.pop('input_id')
    with pytest.raises(PlatformError, match='binding'):
        e.record_observation('valuation-test', 'rule', AT, book['portfolio_id'], payload, AT)
    assert e.repo.get('portfolio_observation', _id('mo', 'valuation-test', 'rule', AT, 'valuation')) is None


def test_legacy_identical_valuation_retry_preserves_metadata_without_backfill(evidence):
    e = evidence
    book, run, payload = ready(e)
    oid = _id('mo', 'valuation-test', 'rule', AT, 'valuation')
    ref = e._artifact('observations', oid, payload)
    legacy = {'schema_version': 'modeling-evidence/1', 'environment': 'beta', 'synthetic': True, 'observation_id': oid, 'experiment_id': 'valuation-test', 'approach_id': 'rule', 'slot': AT, 'portfolio_id': book['portfolio_id'], 'portfolio_revision': 1, 'observed_at': AT, 'observed_key': '2026-01-12T15:00:00.000000Z#' + oid, 'kind': 'valuation', **ref, **e._keys('valuation-test', 'rule', AT, oid)}
    e.repo.commit([PutNew('portfolio_observation', doc=legacy)])
    before = copy.deepcopy(e.repo.require('portfolio_observation', oid).doc)
    result = e.record_observation('valuation-test', 'rule', AT, book['portfolio_id'], payload, AT)
    assert result == before and 'run_id' not in result and 'input_id' not in result
    assert e.repo.require('portfolio_observation', oid).doc == before
