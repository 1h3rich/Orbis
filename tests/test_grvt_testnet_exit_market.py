"""Datos de salida TESTNET: HTTP público sintético, motor e histórico reales."""

from decimal import Decimal

import httpx
import pytest

from tests.test_grvt_filled_strategy_exit import BASE,HOUR,NOW_NS,definition,audited_entry


ROOT='https://market-data.testnet.grvt.io/full/v1'


class Market:
    def __init__(self):
        self.calls=[]; self.clock=NOW_NS; self.mark='98.1'; self.omit_mark=False; self.gap=False
        self.lag=0

    def post(self,url,body):
        self.calls.append((url,body))
        if url==ROOT+'/ticker':
            result={'instrument':'BTC_USDT_Perp','event_time':str(NOW_NS),'last_price':'101.2'}
            if not self.omit_mark: result['mark_price']=self.mark
            return httpx.Response(200,json={'result':result})
        assert url==ROOT+'/kline' and body['type']=='TRADE'
        rows=[{'instrument':'BTC_USDT_Perp','open_time':str(t*1_000_000_000),
            'close_time':str((t+HOUR)*1_000_000_000),'open':'101','high':'102',
            'low':'100','close':'101','volume_b':'10'}
            for t in range(BASE-2*HOUR,BASE+2*HOUR,HOUR)]
        self.clock+=self.lag
        return httpx.Response(200,json={'result':rows[1:] if self.gap else list(reversed(rows)),'next':''})


def test_fetches_mark_and_complete_history_only_from_testnet_before_deciding():
    from app.live_execution.grvt_testnet_exit_market import fetch_testnet_exit_market
    from app.live_execution.grvt_filled_strategy_exit import evaluate_filled_strategy_exit
    from app.lab.engine import CURRENT_ENGINE_VERSION
    venue=Market()
    market=fetch_testnet_exit_market('BTC/USDT:PERP','1h',history_start_t=BASE-2*HOUR,
        public_post=venue.post,clock_ns=lambda:venue.clock)
    order,audit=audited_entry()
    result=evaluate_filled_strategy_exit(definition(),timeframe='1h',engine_version=CURRENT_ENGINE_VERSION,
        order=order,audit=audit,candles=market.candles,mark=market.mark,now_ns=venue.clock)
    assert result.reason=='STOP' and result.mark_price==Decimal('98.1')
    assert market.source=='GRVT_TESTNET_TRADE_AND_MARK' and len(market.history_digest)==64
    assert market.candles[-1].t==BASE+HOUR
    assert all(url.startswith(ROOT+'/') for url,_ in venue.calls)
    assert int(venue.calls[1][1]['end_time'])==(BASE+2*HOUR)*1_000_000_000-1


@pytest.mark.parametrize('problem',['missing_mark','gap','expired_during_history'])
def test_incomplete_or_expired_market_does_not_produce_a_decision(problem):
    from app.live_execution.grvt_testnet_exit_market import fetch_testnet_exit_market
    from app.live_execution.grvt_filled_strategy_exit import GrvtStrategyExitError
    venue=Market(); venue.omit_mark=problem=='missing_mark'; venue.gap=problem=='gap'
    venue.lag=6_000_000_000 if problem=='expired_during_history' else 0
    with pytest.raises(GrvtStrategyExitError):
        fetch_testnet_exit_market('BTC/USDT:PERP','1h',history_start_t=BASE-2*HOUR,
            public_post=venue.post,clock_ns=lambda:venue.clock)


def test_stream_mark_reuses_closed_candles_without_polling_rest_ticker_or_extending_freshness():
    from app.live_execution.grvt_testnet_exit_market import fetch_testnet_exit_market
    from app.live_execution.grvt_filled_strategy_exit import GrvtExitMark,GrvtStrategyExitError
    venue=Market()
    initial=fetch_testnet_exit_market('BTC/USDT:PERP','1h',history_start_t=BASE-2*HOUR,
        public_post=venue.post,clock_ns=lambda:venue.clock)
    venue.clock+=1_000_000_000
    mark=GrvtExitMark('BTC_USDT_Perp',venue.clock,venue.clock,Decimal('100'))
    reused=fetch_testnet_exit_market('BTC/USDT:PERP','1h',history_start_t=BASE-2*HOUR,
        public_post=venue.post,clock_ns=lambda:venue.clock,mark=mark,previous=initial)
    assert len(venue.calls)==2 and reused.mark==mark and reused.candles is initial.candles
    assert reused.history_digest==initial.history_digest and reused.timeframe=='1h'
    venue.clock+=6_000_000_000
    with pytest.raises(GrvtStrategyExitError):
        fetch_testnet_exit_market('BTC/USDT:PERP','1h',history_start_t=BASE-2*HOUR,
            public_post=venue.post,clock_ns=lambda:venue.clock,mark=mark,previous=initial)
    assert len(venue.calls)==2
