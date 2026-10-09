"""Explicit initialization sizes the approved hypothetical plan exactly once."""
from unittest.mock import Mock
import pytest
from scripts.initialize_paper_portfolio import initialize_paper_portfolio

PF="pf_01KDVDNAZ83BAMMYCEGWF33DPM"
PL="pl_01KDVDNAZ83BAMMYCEGWF33DPM"
PV="pv_01KDVDNAZ83BAMMYCEGWF33DPM"
SNAP="snap_01KDVDNAZ83BAMMYCEGWF33DPM"

class Transport:
    def __init__(self):
        self.calls=[];self.saved=None;self.closes={"A":100,"B":200};self.weights=[{"instrument_id":"A","weight":0.5},{"instrument_id":"B","weight":0.5}];self.status="approved";self.publication={"plan_version_id":PV}
    def call(self,method,path,body=None):
        self.calls.append((method,path,body))
        if path==f"/v1/plans/{PL}":return 200,{"plan":{"portfolio_id":PF,"synthetic":True},"current_publication":self.publication}
        if path==f"/v1/portfolios/{PF}/state":
            if method=="PUT":
                assert self.saved is None and body["expected_revision"]==0
                self.saved={"portfolio_id":PF,"revision":1,"paper_state":body["paper_state"],"synthetic":True,"contract_version":"1.3.0"}
                return 200,self.saved
            return (200,self.saved) if self.saved else (422,{"code":"PRECONDITION_FAILED","details":{"reason":"portfolio_state_missing"}})
        if path==f"/v1/plan-versions/{PV}":return 200,{"plan_version":{"plan_id":PL,"synthetic":True,"content":{"base_currency":"USD","allocation":{"weights":self.weights,"cash_weight":0}}}}
        if path.startswith("/v1/snapshots/latest?"):return 200,{"snapshot":{"input_snapshot_id":SNAP,"status":self.status,"coverage":{"end":"2026-10-08"}}}
        if path.startswith(f"/v1/snapshots/{SNAP}/observations?"):return 200,{"observations":[{"instrument_id":i,"close":p,"kind":"completed_daily","session_date":"2026-10-08"} for i,p in self.closes.items()],"next_token":None}
        raise AssertionError((method,path,body))


def ssm():return Mock(get_parameter=Mock(return_value={"Parameter":{"Value":PL}}))


def test_initializer_preview_and_idempotent_apply():
    t=Transport();preview=initialize_paper_portfolio("beta",t,ssm(),dataset_id="finance/research/daily")
    assert preview["preview"] is True and not any(c[0]=="PUT" for c in t.calls)
    result=initialize_paper_portfolio("beta",t,ssm(),dataset_id="finance/research/daily",apply=True)
    state=result["state"]["paper_state"]
    assert result["created"] is True and state["positions"]==[{"instrument_id":"A","quantity":50},{"instrument_id":"B","quantity":25}]
    assert state["high_watermark"]==10000 and state["cash_balance"]==0 and state["input_snapshot_id"]==SNAP and state["source_plan_version_id"]==PV
    again=initialize_paper_portfolio("beta",t,ssm(),dataset_id="finance/research/daily",apply=True)
    assert again["created"] is False and again["state"]==result["state"] and len([c for c in t.calls if c[0]=="PUT"])==1


@pytest.mark.parametrize("change",[lambda t:t.closes.pop("B"),lambda t:t.closes.update(A=0),lambda t:setattr(t,"status","committed"),lambda t:setattr(t,"publication",None),lambda t:t.weights[0].update(weight=0.7)])
def test_initializer_invalid_plan_or_prices_never_write(change):
    t=Transport();change(t)
    with pytest.raises(ValueError):initialize_paper_portfolio("beta",t,ssm(),dataset_id="finance/research/daily",apply=True)
    assert not any(c[0]=="PUT" for c in t.calls)


def test_initializer_is_beta_only():
    with pytest.raises(ValueError):initialize_paper_portfolio("prod",Transport(),ssm(),dataset_id="finance/research/daily",apply=True)
