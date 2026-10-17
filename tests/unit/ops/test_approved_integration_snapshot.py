"""An unapproved lifecycle probe cannot poison downstream inference's SSM pointer."""
from urllib.parse import parse_qs,urlsplit
import pytest
from tests.integration.approved_snapshot import publish_approved_integration_snapshot
from tests.integration.lifecycle_suite import IntegrationError

CANDIDATE='snap_01JA2B3C4D5E6F7G8H9JKMNPQR'
APPROVED='snap_01JA2B3C4D5E6F7G8H9JKMNPRS'
DATASET='finance/etf-daily/SPY'


def snapshot(ident,status,*,dataset=DATASET):
    return 200,{'snapshot':{'input_snapshot_id':ident,'status':status,'dataset':{'dataset_id':dataset}}},{}


class Transport:
    def __init__(self,*responses):self.responses=list(responses);self.calls=[]
    def call(self,method,path):self.calls.append((method,path));return self.responses.pop(0)


class Ssm:
    def __init__(self):self.writes=[]
    def put_parameter(self,**kwargs):self.writes.append(kwargs)


def publish(t,s):
    return publish_approved_integration_snapshot(t,s,env='beta',dataset_id=DATASET,candidate_id=CANDIDATE)


def test_committed_lifecycle_falls_back_to_verified_approved_dataset_only():
    t=Transport(snapshot(CANDIDATE,'committed'),snapshot(APPROVED,'approved'));s=Ssm()
    assert publish(t,s)==APPROVED
    assert [w['Value'] for w in s.writes]==[APPROVED]
    assert CANDIDATE not in [w['Value'] for w in s.writes]
    method,path=t.calls[1]
    assert method=='GET' and path.startswith('/v1/snapshots/latest?')
    assert 'finance%2Fetf-daily%2FSPY' in path
    assert parse_qs(urlsplit(path).query)=={'dataset_id':[DATASET]}


def test_already_approved_candidate_needs_no_fallback():
    t=Transport(snapshot(CANDIDATE,'approved'));s=Ssm()
    assert publish(t,s)==CANDIDATE and len(t.calls)==1
    assert s.writes==[{'Name':'/finplan/beta/financialplanning/config/integration-snapshot-id','Value':CANDIDATE,'Type':'String','Overwrite':True}]


@pytest.mark.parametrize('fallback',[
    snapshot(APPROVED,'committed'),snapshot(APPROVED,'approved',dataset='finance/etf-daily/QQQ'),
    snapshot('snap_invalid','approved'),(404,{'code':'NOT_FOUND'},{}),
    (200,{},{}),
])
def test_invalid_or_missing_approved_fallback_fails_without_writing(fallback):
    t=Transport(snapshot(CANDIDATE,'committed'),fallback);s=Ssm()
    with pytest.raises(IntegrationError):publish(t,s)
    assert s.writes==[]


def test_mismatched_candidate_cannot_redirect_the_consumer_pointer():
    t=Transport(snapshot(APPROVED,'approved'));s=Ssm()
    with pytest.raises(IntegrationError,match='different candidate'):publish(t,s)
    assert not s.writes
