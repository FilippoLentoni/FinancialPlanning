"""Independent controlled packaging tests; no AWS clients or model invocations."""
import base64
import copy
import json
from pathlib import Path

import pytest

from finplan_platform.reporting import transport as t


def test_original_lexemes_are_not_reserialized_and_wrong_historical_tokens_fail():
    # These byte representations differ from ordinary JS/Python value rendering.
    first = b' {"run_id":"old","negative_zero":-0.0,"e":1e-07,"n":1.0000,"text":"\\u00e9\\/\\\\", "observation":[3,1,3]}\n'
    second = first.replace(b'"old"', b'"new"').replace(b'[3,1,3]', b'[4,1,3]')
    pool, lexemes = {}, t.Lexemes()
    a, b = t.pack_json_bytes(first,pool,lexemes), t.pack_json_bytes(second,pool,lexemes)
    assert t.unpack_json_bytes(a,pool,lexemes.values) == first
    assert t.unpack_json_bytes(b,pool,lexemes.values) == second
    assert a['checksum'] != b['checksum']
    assert b'-0.0' in first and b'1.0000' in first
    changed = copy.deepcopy(lexemes.values)
    index = next(i for i,v in enumerate(changed) if base64.b64decode(v) == b'"old"')
    changed[index] = base64.b64encode(b'"new"').decode()
    with pytest.raises(ValueError,match='checksum'):
        t.unpack_json_bytes(a,pool,changed)


def test_total_budget_rejection_preserves_existing_immutable_and_latest_reports(tmp_path,monkeypatch):
    from finplan_platform.reporting.reports import write_report
    from test_modeling_reports import report
    destination = tmp_path/'reports'
    original = report()
    written = write_report(original,destination)
    before = {str(p.relative_to(destination)):p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    original['runs'][0]['rationale'] = 'Later report with original evidence'
    monkeypatch.setattr(t,'MAX_TRANSPORT_BYTES',100)
    with pytest.raises(ValueError,match='bound|limit|truncated'):
        write_report(original,destination)
    after = {str(p.relative_to(destination)):p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    assert after == before
    assert Path(written['archive'],'report.html').exists()


def test_explicit_input_byte_limit_checks_before_original_token_expansion(monkeypatch):
    raw = b'{"large":"'+b'x'*400+b'"}'
    pool, lexemes = {},t.Lexemes()
    capture = t.pack_json_bytes(raw,pool,lexemes)
    monkeypatch.setattr(t,'MAX_ARTIFACT_BYTES',20)
    with pytest.raises(ValueError,match='limit'):
        t.unpack_json_bytes(capture,pool,lexemes.values)


def test_artifact_ref_names_do_not_reinterpret_ordinary_model_forecast_fields():
    from test_modeling_reports import report
    original = report()
    original['forecasts'] = [{'forecast_id':'literal_fields','assumptions':{'raw_chunks':['literal value'],'lexeme_chunks':[3,2,1]}}]
    decoded = t.decode_transport(t.encode_transport(original))
    assert decoded['forecasts'] == original['forecasts']
