"""An adapter's cached shared checksum must bind to the original persisted bytes."""
import hashlib

import pytest

from finplan_platform.core.errors import PlatformError
from finplan_platform.core.modeling_evidence import ModelingEvidence, _id

AT = '2026-01-12T15:00:00Z'


@pytest.fixture
def evidence(repo_any, artifacts):
    return ModelingEvidence(repo_any, artifacts)


def test_cached_shared_checksum_binds_without_reserializing_or_replacing_source(evidence):
    e = evidence
    shared = e.capture_shared_source('binding-test', AT, {'availability_cutoff': AT, 'prices': {'SPY': [98., 99., 100.]}}, AT)
    original = e.read_input_bytes(shared['input_id'])
    supplied = e.capture_input('binding-test', 'rule', AT, {'availability_cutoff': AT, 'shared_source_checksum': shared['checksum'], 'numeric': {'prepared': {'ordered_returns': [.01, .0101010101]}}}, shared, AT)
    assert supplied['shared_source_ref']['checksum'] == 'sha256:' + hashlib.sha256(original).hexdigest()
    assert e.read_input(supplied['input_id'])['payload']['shared_source_checksum'] == shared['checksum']
    assert e.read_input_bytes(shared['input_id']) == original


@pytest.mark.parametrize('wrong', ['sha256:' + 'a' * 64, None, ''])
def test_wrong_shared_hash_fails_before_artifact_run_or_inference(evidence, monkeypatch, wrong):
    e = evidence
    shared = e.capture_shared_source('binding-test', AT, {'availability_cutoff': AT, 'prices': {'SPY': [98., 99., 100.]}}, AT)
    before = e.read_input_bytes(shared['input_id'])
    calls = []
    monkeypatch.setattr(e, '_artifact', lambda *a, **k: pytest.fail('Invalid source binding reached artifact publication'))
    def model_boundary():
        e.capture_input('binding-test', 'rule', AT, {'availability_cutoff': AT, 'shared_source_checksum': wrong, 'numeric': {'prepared': {'ordered_returns': [.01, .0101010101]}}}, shared, AT)
        calls.append('inference')
    with pytest.raises(PlatformError, match='authoritative source'):
        model_boundary()
    assert calls == []
    assert e.repo.get('research_input', _id('mi', 'binding-test', 'rule', AT, 'model_input')) is None
    assert e.repo.get('research_run', _id('mr', 'binding-test', 'rule', AT)) is None
    assert e.read_input_bytes(shared['input_id']) == before
