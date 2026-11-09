"""Strict live market windows; no future bars or invented quote timestamps."""
from datetime import timedelta
from decimal import Decimal
import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_market import reference_quote,advance_market,warmup_market
from tests.test_dex_bot_plan import NOW,bars


def test_reference_quote_uses_provider_nanoseconds_without_float_rounding():
    ns=int(NOW.timestamp())*10**9+123456000
    received=NOW+timedelta(microseconds=200000)
    quote=reference_quote({'symbol':'BNB/USDT','source':'mexc-spot-public','event_time_ns':ns,
        'bid':'123.123456789123456789','ask':'123.123456789123456790','last':'123.12'},observed_at=received)
    assert quote.bid==Decimal('123.123456789123456789')
    assert quote.event_at==NOW+timedelta(microseconds=123456)
    assert quote.source=='mexc-spot-public'


@pytest.mark.parametrize('change',[{'event_time_ns':True},{'symbol':'BTC/USDT'},{'source':'other'},
    {'bid':'NaN'},{'ask':'1'},{'event_time_ns':int((NOW+timedelta(seconds=1)).timestamp())*10**9}])
def test_bad_reference_never_becomes_a_trading_quote(change):
    raw={'symbol':'BNB/USDT','source':'mexc-spot-public','event_time_ns':int(NOW.timestamp())*10**9,
        'bid':'100','ask':'101','last':'100'}
    with pytest.raises(DexExecutorError):reference_quote({**raw,**change},observed_at=NOW)


def test_contiguous_window_keeps_closed_overlap_and_ignores_the_forming_bar():
    history=bars()[:-1];incoming=[bar.json() for bar in bars()[-3:]]
    incoming.append({**incoming[-1],'t':incoming[-1]['t']+60})
    updated=advance_market(history,incoming,timeframe='1m',now=NOW)
    assert updated.ready and updated.candles==bars()
    assert history==bars()[:-1]


def test_late_close_waits_briefly_but_missing_coverage_or_revised_history_is_rejected():
    history=bars()[:-1];overlap=[bar.json() for bar in history[-2:]]
    pending=advance_market(history,overlap,timeframe='1m',now=NOW)
    assert not pending.ready and pending.candles==history
    for raw,at in ((overlap,NOW+timedelta(seconds=16)),
                  ([{**row,'c':'999'} for row in overlap],NOW),
                  ([bar.json() for bar in bars()[-3:]],NOW+timedelta(minutes=1))):
        with pytest.raises(DexExecutorError):advance_market(history,raw,timeframe='1m',now=at)


def test_volume_market_rechecks_all_prior_volumes_during_warmup_and_live():
    history=bars()[:-1];rows=[bar.json() for bar in bars()]
    raw=rows[-4:]
    assert advance_market(history,raw,timeframe='1m',now=NOW,overlap_count=3).candles==bars()
    assert not advance_market(history,raw[:-1],timeframe='1m',now=NOW,overlap_count=3).ready
    revised=[{**raw[0],'v':'999'},*raw[1:]]
    with pytest.raises(DexExecutorError,match='revisadas'):
        advance_market(history,revised,timeframe='1m',now=NOW,overlap_count=3)
    class Market:
        def get_candles(self,symbol,*,since,before,limit,**_):
            assert (symbol,since,before,limit)==('BNB/USDT',raw[0]['t'],raw[-1]['t']+60,4)
            return revised
    with pytest.raises(DexExecutorError,match='histórico inicial'):
        warmup_market(rows[:-1],Market(),timeframe='1m',clock=lambda:NOW,overlap_count=3)


def test_initial_warmup_catches_up_without_treating_past_candles_as_entries():
    history=bars();seed=[bar.json() for bar in history[:-2]];calls=[]
    class Market:
        def get_candles(self,symbol,*,timeframe,since,before,limit):
            calls.append((symbol,timeframe,since,before,limit))
            return [bar.json() for bar in history if since<=bar.t<before]
    result=warmup_market(seed,Market(),timeframe='1m',clock=lambda:NOW)
    assert result==[bar.json() for bar in history] and result[:len(seed)]==seed and len(calls)==1


def test_mexc_warmup_pages_close_at_500_bars_with_two_bar_overlap():
    end = int(NOW.timestamp())//60*60
    history = [{"t":end-(1100-index)*60,"o":"100","h":"101","l":"99","c":"100","v":"1"}
               for index in range(1100)]
    calls = []
    class Market:
        def get_candles(self,symbol,*,timeframe,since,before,limit):
            calls.append((since,before,limit))
            assert symbol=='BNB/USDT' and timeframe=='1m' and 3<=limit<=500
            assert before==since+limit*60
            return [row for row in history if since<=row['t']<before]

    assert warmup_market(history[:12],Market(),timeframe='1m',clock=lambda:NOW)==history
    assert [limit for _,_,limit in calls]==[500,500,94]
    assert all(current[0]==previous[1]-120 for previous,current in zip(calls,calls[1:]))
    assert calls[-1][1]==end


@pytest.mark.parametrize('fault',['revised','gap','not-published','future','cancelled'])
def test_initial_warmup_rejects_unreproducible_data_or_cancellation(fault):
    rows=[bar.json() for bar in bars()];seed=rows[:-2]
    class Market:
        def get_candles(self,_,*,since,**kwargs):
            selected=[dict(row) for row in rows if row['t']>=since]
            if fault=='revised':selected[0]['c']='999'
            if fault=='gap':selected.pop(2)
            if fault=='not-published':selected=selected[:2]
            if fault=='future':selected.append({**selected[-1],'t':selected[-1]['t']+120})
            return selected
    def active():
        if fault=='cancelled':raise DexExecutorError('cancelled')
    with pytest.raises(DexExecutorError):warmup_market(seed,Market(),timeframe='1m',clock=lambda:NOW,require_active=active)
