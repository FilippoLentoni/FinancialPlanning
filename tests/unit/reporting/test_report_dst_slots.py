"""Seven New York calendar days include UTC DST fold/gap opportunities."""
import pytest
from finplan_platform.reporting.reports import aggregate
from test_modeling_reports import Reader,registry


@pytest.mark.parametrize('start,end,first,generated,expected',[
    ('2026-10-26','2026-11-01','2026-10-26T04:00:00Z','2026-11-02T12:00:00Z',169),
    ('2026-03-02','2026-03-08','2026-03-02T05:00:00Z','2026-03-09T12:00:00Z',167),
])
def test_every_declared_hour_is_accounted_across_dst(start,end,first,generated,expected):
    reader=Reader();reader.runs=[];reader.observations=[]
    result=aggregate(reader,experiment_id='test',registry=registry(),start_day=start,end_day=end,
                     generated_at=generated,mode='controlled_replay',comparison_policy={
                         'first_scheduled_slot':first,'cadence_seconds':3600,'decision_offset_seconds':300})
    slots={row['scheduled_slot']for row in result['missing_slots']}
    assert len(slots)==expected and len(result['missing_slots'])==2*expected
    assert result['runs']==[] and result['inputs']=={}
    if expected==169:
        assert '2026-11-01T05:00:00+00:00' in slots and '2026-11-01T06:00:00+00:00' in slots
