"""Completed UTC crypto candles, isolated from daily equity snapshots."""
from __future__ import annotations
import hashlib
import json
import math
import urllib.request
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from .artifacts import ArtifactExists, ArtifactNotFound
from .context import Caller, OperationContext
from .errors import PlatformError
from .ingestion_budget import SsmBudgetGate
from .upgrade import current_version

PRODUCTS = ('BTC-USD', 'ETH-USD')
DATASET = 'coinbase-crypto-hourly-v1'
HISTORY = 168


def stamp(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def hour(value):
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def key(end):
    return 'crypto-hourly/v1/' + end.strftime('%Y/%m/%d/%H') + '.json'


def normalize(rows, *, start, end):
    expected = list(range(int(start.timestamp()), int(end.timestamp()), 3600))
    found = {}
    if not isinstance(rows, list) or len(rows) > 300:
        raise ValueError('invalid candle response')
    for row in rows:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError('invalid candle shape')
        t, low, high, opened, close, volume = row
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t != int(t) or int(t) % 3600:
            raise ValueError('invalid hourly timestamp')
        if any(isinstance(x, bool) or not isinstance(x, (int,float)) or not math.isfinite(x) for x in row):
            raise ValueError('invalid candle number')
        if min(low, opened, close) <= 0 or not low <= min(opened, close) <= max(opened, close) <= high or volume < 0:
            raise ValueError('invalid candle prices')
        if int(t) not in expected:
            continue  # provider may return older bars and the currently open bar
        value = {'start': stamp(datetime.fromtimestamp(t, UTC)), 'open': opened, 'close': close, 'low': low, 'high': high, 'volume': volume}
        if int(t) in found and found[int(t)] != value:
            raise ValueError('conflicting candle revision')
        found[int(t)] = value
    if sorted(found) != expected:
        raise ValueError('missing aligned hourly candles')
    return [found[t] for t in expected]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(product, start, end):
    assert product in PRODUCTS
    url = 'https://api.exchange.coinbase.com/products/' + product + '/candles?' + urlencode({'granularity': 3600, 'start': stamp(start), 'end': stamp(end)})
    request = urllib.request.Request(url, headers={'Accept':'application/json','User-Agent':'finplan-hourly-research/1'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError('oversized candle response')
    rows = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    return rows, 'sha256:' + hashlib.sha256(raw).hexdigest()


def collect(ctx, svc, *, provider=fetch, end=None):
    if svc.env != 'beta':
        raise PlatformError('OPERATION_NOT_PERMITTED', 'hourly crypto research is beta only')
    SsmBudgetGate(svc.extras['ssm'])(ctx)
    end = hour(end or ctx.clock.now())
    if end > hour(ctx.clock.now()) or (hour(ctx.clock.now())-end).total_seconds()>3600:
        raise PlatformError("PRECONDITION_FAILED","hourly collection slot is future dated or expired")
    try:
        raw, _ = svc.store.get('raw', key(end))
        return json.loads(raw)
    except ArtifactNotFound:
        pass
    start = end - timedelta(hours=HISTORY)
    bars, sources = {}, []
    for product in PRODUCTS:
        source_key=key(end).removesuffix('.json')+'/'+product+'.json'
        try:
            frozen,_=svc.store.get('raw',source_key)
            evidence=json.loads(frozen);rows,digest=evidence['rows'],evidence['response_checksum']
        except ArtifactNotFound:
            rows,digest=provider(product,start,end)
        try:
            bars[product] = normalize(rows, start=start, end=end)
        except ValueError:
            raise PlatformError('DEPENDENCY_UNAVAILABLE','crypto provider returned invalid or incomplete candles', retryable=True) from None
        source_data=json.dumps({'rows':rows,'response_checksum':digest},sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        source=svc.store.put_once_or_verify('raw',key(end).removesuffix('.json')+'/'+product+'.json',source_data)
        sources.append({'product':product,'provider':'coinbase_exchange_public','response_checksum':digest,'frozen_source_checksum':source.checksum})
    doc = {'dataset_id':DATASET, 'as_of':stamp(end), 'available_at':stamp(ctx.clock.now()), 'granularity_seconds':3600, 'market_data_synthetic':False,
           'products':list(PRODUCTS),'bars':bars,'sources':sources,'contract_version':current_version()}
    data = json.dumps(doc, sort_keys=True, separators=(',',':'), allow_nan=False).encode()
    try:
        svc.store.put_once('raw', key(end), data)
    except ArtifactExists:
        raw, _ = svc.store.get('raw', key(end)); doc = json.loads(raw)
    return doc


def latest(ctx, svc):
    if svc.env != 'beta':
        raise PlatformError('OPERATION_NOT_PERMITTED','hourly crypto research is beta only')
    end = hour(ctx.clock.now())
    for offset in (0, 1):
        try:
            raw, artifact = svc.store.get('raw', key(end - timedelta(hours=offset)))
        except ArtifactNotFound:
            continue
        doc = json.loads(raw)
        if (ctx.clock.now() - datetime.fromisoformat(doc['available_at'].replace('Z','+00:00'))).total_seconds() > 5400:
            break
        return {**doc, 'dataset_checksum':artifact.checksum}
    raise PlatformError('DEPENDENCY_UNAVAILABLE','no fresh completed hourly crypto snapshot is available', retryable=True)


def scheduled(event):
    from .services import Services
    from .clock import SystemClock
    svc=Services.from_lambda_environment()
    ctx=OperationContext(caller=Caller(principal='hourly-crypto-scheduler', channel='scheduler'),env=svc.env,correlation_id='crypto-hourly-schedule',clock=SystemClock(),trigger='scheduled')
    scheduled_at=event.get("scheduled_at")
    slot=datetime.fromisoformat(scheduled_at.replace("Z","+00:00")) if scheduled_at else None
    doc=collect(ctx,svc,end=slot)
    return {k:doc[k] for k in ('dataset_id','as_of','products','available_at')}
