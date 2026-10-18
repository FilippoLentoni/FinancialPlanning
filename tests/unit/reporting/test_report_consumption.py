"""CLI-only ephemeral memory release preserves every published value and byte."""
import copy
from pathlib import Path

import pytest
from finplan_platform.reporting import reports
from finplan_platform.reporting.reports import captured_input_bytes
from test_modeling_reports import report


def test_consuming_export_matches_default_and_keeps_postwrite_query_fields(tmp_path):
    original = report(); baseline = copy.deepcopy(original)
    normal = reports.write_report(original, tmp_path/'pure')
    assert original == baseline
    consumed = copy.deepcopy(baseline)
    written = reports.write_report(consumed, tmp_path/'owned', consume_read_model=True)
    assert consumed['read_model_compacted'] is True
    assert len(consumed['runs']) == len(baseline['runs'])
    for key in ('evidence_failures','missing_slots','leaderboard','source_record_ids','end_day','start_day','period'):
        assert consumed[key] == baseline[key]
    for run_id, capture in consumed['inputs'].items():
        assert captured_input_bytes(consumed,capture) == captured_input_bytes(baseline,baseline['inputs'][run_id])
    assert written['revision'] == normal['revision']
    for name in ('report.json','report.csv','report.html','manifest.json'):
        assert (Path(written['archive'])/name).read_bytes() == (Path(normal['archive'])/name).read_bytes()


def test_consumption_releases_raw_model_only_after_successful_compaction(monkeypatch,tmp_path):
    original = report(); baseline = copy.deepcopy(original)
    def fail(_): raise ValueError('controlled compaction failure')
    monkeypatch.setattr(reports,'compact_report',fail)
    with pytest.raises(ValueError,match='controlled compaction'):
        reports.write_report(original,tmp_path,consume_read_model=True)
    assert original == baseline
    assert not list(tmp_path.rglob('latest.*'))


def test_consumed_model_is_released_before_outer_encoding(monkeypatch,tmp_path):
    original=report(); real=reports.encode_transport; checked=[]
    def encode(value):
        if not value.get('transport_codec'):
            assert value is original
            assert value['read_model_compacted'] is True
            assert all('source_evidence_ref'in row for row in value['runs'])
            checked.append(True)
        return real(value)
    monkeypatch.setattr(reports,'encode_transport',encode)
    reports.write_report(original,tmp_path,consume_read_model=True)
    assert checked == [True]
