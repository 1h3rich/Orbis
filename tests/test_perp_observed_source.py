"""Public collection: original prefix, boundary ordering and missing data."""
import json

import pytest

from app.derivatives.funding import HOUR_NS
from app.derivatives.perp_observed import observe
from app.derivatives.perp_observed_source import GrvtObservedSource
from app.derivatives.perp_observed_types import ObservedError
from tests.perp_observed_archive_fixture import NS,owned_archive
from tests.perp_observed_source_fixture import OwnedPublicAdapter


def source_for(seed,clock):
    adapter=OwnedPublicAdapter(seed,clock)
    return GrvtObservedSource(adapter=adapter,clock_ns=lambda:clock['now']),adapter


def test_prepare_extends_exact_original_prefix_without_trading(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    source,adapter=source_for(seed,clock)
    actual=source.prepare(origin);state=actual.start().state
    prefix=json.loads(origin.trade_prefix_json)
    assert [row.model_dump() for row in actual.history[:len(prefix)]]==prefix
    assert len(actual.history)==len(seed.history) and state.account.cash==1000
    assert state.opened is None and state.order_count==0
    assert adapter.calls[-1]=='quote' and 'funding' in adapter.calls


def test_poll_only_fetches_new_closed_candles_and_preserves_metadata_times(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    state=seed.start().state;source,adapter=source_for(seed,clock)
    for second in range(10,61,10):
        clock['now']=state.started_at_ns+second*NS
        if second==60:
            stamp=state.started_at_ns//NS
            adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
        event=source.poll(state);state=observe(state,event.batch()).state
    assert state.opened is not None and state.order_count==1
    assert adapter.calls.count('candles')==1 and adapter.calls.count('funding')==0
    assert adapter.calls.count('rules')==2
    assert state.account.markets[0].rules.fetched_at_ns==state.started_at_ns+40*NS


@pytest.mark.parametrize('change',['missing','duplicate','changed_overlap','open_candle'])
def test_new_candle_batch_rejects_incomplete_or_changed_provider_data(tmp_path,monkeypatch,change):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    state=seed.start().state;source,adapter=source_for(seed,clock)
    stamp=state.started_at_ns//NS;clock['now']=state.started_at_ns+60*NS
    adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
    original=adapter.get_historical_candles
    def altered(*args,**kwargs):
        rows=original(*args,**kwargs)
        if change=='missing':return rows[:-1]
        if change=='duplicate':return rows+[rows[-1]]
        if change=='open_candle':return rows+[{**rows[-1],'t':stamp+60}]
        rows[0]['v']='999';return rows
    monkeypatch.setattr(adapter,'get_historical_candles',altered)
    with pytest.raises(ObservedError,match='Velas públicas|histórico cerrado'):source.poll(state)


def test_failed_refresh_never_rejuvenates_cached_rules(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    state=seed.start().state;source,adapter=source_for(seed,clock)
    clock['now']+=NS;source.poll(state);old=source._metadata_cache['BTC/USDT:PERP']
    clock['now']+=30*NS;adapter.fail='specification'
    with pytest.raises(ValueError):source.poll(state)
    assert source._metadata_cache['BTC/USDT:PERP']==old


def test_funding_boundary_requires_actual_new_payment_and_same_anchor(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    state=seed.start().state;source,adapter=source_for(seed,clock)
    anchor=state.funding_anchor;due=anchor.time_ns+HOUR_NS
    clock['now']=due+NS
    records=source._funding('BTC/USDT:PERP',clock['now'],anchor)
    assert records==(anchor,)  # Core will reject incomplete coverage; no zero row fabricated.
    adapter.funding.append({**adapter.funding[-1],'funding_time':str(due),'funding_rate':'0.01'})
    records=source._funding('BTC/USDT:PERP',clock['now'],anchor)
    assert len(records)==2 and records[-1].time_ns==due and str(records[-1].rate_percent)=='0.01'
    adapter.funding[0]['funding_rate']='0.02'
    with pytest.raises(ObservedError,match='ancla'):source._funding('BTC/USDT:PERP',clock['now'],anchor)


def test_future_or_stale_quote_cannot_create_an_observed_seed(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    source,adapter=source_for(seed,clock)
    for stamp in (clock['now']+NS,clock['now']-16*NS):
        adapter.quote_override={'event_time_ns':stamp}
        with pytest.raises(ValueError):source.prepare(origin)
    assert archive.page()['total']==0


def test_prepare_crossing_a_candle_boundary_recollects_without_seed_orders(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    source,adapter=source_for(seed,clock);stamp=clock['now']//NS
    adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
    original=adapter.get_ticker;calls=[]
    def crossing(symbol):
        if not calls:clock['now']+=60*NS
        calls.append(True);return original(symbol)
    monkeypatch.setattr(adapter,'get_ticker',crossing)
    result=source.prepare(origin)
    assert result.history[-1].t==stamp and len(calls)==2
    assert result.start().state.order_count==0


def test_funding_boundary_crossed_during_quote_fetches_settlement_and_new_quote(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    source,adapter=source_for(seed,clock);anchor=seed.start().state.funding_anchor
    due=anchor.time_ns+HOUR_NS;clock['now']=due-NS
    adapter.funding.append({**adapter.funding[-1],'funding_time':str(due),'funding_rate':'0.02'})
    adapter.advance_quote_ns=2*NS
    quote,received,rules,spec,records=source._point('BTC/USDT:PERP',anchor)
    assert records[-1].time_ns==due and records[-1].rate_percent>0
    assert adapter.calls.count('funding')==1 and adapter.calls.count('quote')==2
    assert received==quote.event_time_ns==due+3*NS


def test_production_public_adapter_transport_collects_and_executes_the_same_book(tmp_path,monkeypatch):
    from app.marketdata.adapters.grvt import GrvtAdapter
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    owned=OwnedPublicAdapter(seed,clock);requests=[]
    monkeypatch.setattr('app.marketdata.adapters.grvt.time.time_ns',lambda:clock['now'])
    def post(url,body):
        endpoint=url.rsplit('/',1)[-1];requests.append(endpoint)
        symbol='BTC/USDT:PERP';instrument=seed.quote.instrument
        assert body['instrument']==instrument
        if endpoint=='margin_rules':return owned.get_margin_rules(symbol)
        if endpoint=='instrument':return {'result':owned.get_instrument_spec(symbol)}
        if endpoint=='ticker':
            quote=owned.get_ticker(symbol)
            return {'result':{'instrument':instrument,'event_time':str(quote['event_time_ns']),'last_price':'100',
                'mark_price':quote['mark_price'],'best_bid_price':quote['bid'],'best_ask_price':quote['ask']}}
        if endpoint=='funding':
            return {'result':[row for row in reversed(owned.funding)
                if int(body['start_time'])<=int(row['funding_time'])<=int(body['end_time'])]}
        assert endpoint=='kline' and body['type']=='TRADE'
        return {'result':[{'instrument':instrument,'open_time':str(stamp*NS),'close_time':str((stamp+60)*NS),
            'open':row['o'],'high':row['h'],'low':row['l'],'close':row['c'],'volume_b':row['v']}
            for stamp,row in sorted(owned.rows.items())
            if int(body['start_time'])<=stamp*NS<=int(body['end_time'])]}
    source=GrvtObservedSource(adapter=GrvtAdapter(http_post=post),clock_ns=lambda:clock['now'])
    actual=source.prepare(origin);state=actual.start().state
    for second in range(10,61,10):
        clock['now']=int(actual.received_at_ns)+second*NS
        if second==60:
            stamp=int(actual.received_at_ns)//NS
            owned.rows[stamp]={**actual.history[-1].model_dump(),'t':stamp}
        state=observe(state,source.poll(state).batch()).state
    assert state.order_count==1 and state.opened is not None
    assert set(requests)=={'kline','margin_rules','instrument','funding','ticker'}
