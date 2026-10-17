from datetime import UTC, datetime, timedelta
import pytest
from finplan_platform.core.crypto_hourly import normalize, key


def test_completed_candles_exclude_future_and_require_contiguous_history():
    start=datetime(2026,1,1,tzinfo=UTC);end=start+timedelta(hours=2)
    rows=[[int((start+timedelta(hours=i)).timestamp()),1,3,2,2.5,10] for i in range(3)]
    bars=normalize(rows,start=start,end=end)
    assert len(bars)==2
    with pytest.raises(ValueError,match='missing'):normalize(rows[1:],start=start,end=end)
    assert key(end).endswith('2026/01/01/02.json')


@pytest.mark.parametrize('row',[[1767225600,0,3,2,2.5,10],[1767225600,1,3,2,float('nan'),10],[1767225601,1,3,2,2.5,10]])
def test_invalid_prices_and_timestamps_fail(row):
    start=datetime(2026,1,1,tzinfo=UTC)
    with pytest.raises(ValueError):normalize([row],start=start,end=start+timedelta(hours=1))


def test_collection_replays_frozen_hour_without_provider_calls(monkeypatch):
    import json
    from types import SimpleNamespace
    from finplan_platform.core import crypto_hourly as crypto
    from finplan_platform.core.artifacts import ArtifactNotFound
    now=datetime(2026,1,9,3,5,tzinfo=UTC)
    objects={};calls=[]
    class Store:
        def get(self,role,key):
            if key not in objects:raise ArtifactNotFound(role)
            return objects[key],SimpleNamespace(checksum='sha256:'+'a'*64)
        def put_once(self,role,key,data):
            assert key not in objects
            objects[key]=data
            return SimpleNamespace(checksum='sha256:'+'a'*64)
        def put_once_or_verify(self,role,key,data):
            if key in objects:assert objects[key]==data
            else:objects[key]=data
            return SimpleNamespace(checksum='sha256:'+'a'*64)
    def provider(product,start,end):
        calls.append(product)
        return [[int((start+timedelta(hours=i)).timestamp()),1,3,2,2.5,10] for i in range(168)],'sha256:'+'b'*64
    monkeypatch.setattr(crypto,'SsmBudgetGate',lambda _:lambda ctx:None)
    ctx=SimpleNamespace(clock=SimpleNamespace(now=lambda:now));svc=SimpleNamespace(env='beta',extras={'ssm':None},store=Store())
    first=crypto.collect(ctx,svc,provider=provider)
    second=crypto.collect(ctx,svc,provider=provider)
    assert first==second and calls==list(crypto.PRODUCTS)
    with pytest.raises(Exception):crypto.collect(ctx,svc,provider=provider,end=now+timedelta(hours=1))
