"""Lectura puntual de mercado para el evaluador de salidas TESTNET.

Reutiliza el paginador GRVT, restringiendo el transporte al kline TESTNET.
No usa la caché mainnet de PAPER. El inicio explícito se debe conservar
como parte de la definición del observador, sin desplazarlo entre consultas.
"""

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Literal

import httpx

from app.lab.engine import Candle,INTERVALS,validate_candles
from app.live_execution.grvt_filled_strategy_exit import GrvtExitMark,GrvtStrategyExitError
from app.live_execution.grvt_testnet_oco import _encode
from app.live_execution.grvt_testnet_quote import _decimal,_post
from app.marketdata.adapters.grvt import GrvtAdapter


_ROOT='https://market-data.testnet.grvt.io/full/v1'


@dataclass(frozen=True,slots=True)
class GrvtTestnetExitMarket:
    mark: GrvtExitMark
    candles: tuple[Candle,...]
    history_start_t: int
    history_end_t: int
    history_digest: str
    timeframe: str
    source: Literal['GRVT_TESTNET_TRADE_AND_MARK']='GRVT_TESTNET_TRADE_AND_MARK'


def fetch_testnet_exit_market(symbol: str,timeframe: str,*,history_start_t: int,
        public_post: Callable[[str,dict],httpx.Response]=_post,
        clock_ns: Callable[[],int]=time.time_ns,mark: GrvtExitMark|None=None,
        previous: GrvtTestnetExitMarket|None=None) -> GrvtTestnetExitMarket:
    """Ticker con mark obligatorio + todas las velas cerradas [inicio, fin).

    El rango se cierra en el timestamp del mark para no introducir velas
    posteriores a la decisión. Una descarga lenta invalida toda la revisión.
    Los 5000 intervalos son un límite explícito, nunca un truncado silencioso.
    """
    try:
        step=INTERVALS[timeframe]
        adapter=GrvtAdapter()
        instrument=adapter._instrument(symbol)
        if instrument is None or type(history_start_t) is not int or history_start_t%step:
            raise ValueError('invalid market scope')
        if mark is None:
            response=public_post(_ROOT+'/ticker',{'instrument':instrument})
            payload=response.json() if response.status_code==200 else None
            row=payload['result']
            at=clock_ns(); raw=row['event_time']
            if (not isinstance(payload,dict) or 'error' in payload or 'code' in payload
                    or row['instrument']!=instrument or not isinstance(raw,str) or not raw.isascii()
                    or not raw.isdecimal() or not 1<=len(raw)<=20):
                raise ValueError('invalid mark timestamp')
            mark=GrvtExitMark(instrument,int(raw),at,_decimal(row['mark_price']))
        at=clock_ns()
        if (not isinstance(mark,GrvtExitMark) or mark.instrument!=instrument
                or type(mark.event_time_ns) is not int or type(mark.observed_at_ns) is not int
                or type(at) is not int or not 0<mark.event_time_ns<=mark.observed_at_ns<=at
                or at-mark.event_time_ns>5_000_000_000 or _decimal(format(mark.price,'f'))!=mark.price):
            raise ValueError('expired mark')
        end=mark.event_time_ns//1_000_000_000//step*step
        if (previous is not None and previous.timeframe==timeframe
                and previous.history_start_t==history_start_t and previous.history_end_t==end
                and previous.source=='GRVT_TESTNET_TRADE_AND_MARK' and previous.mark.instrument==instrument):
            return GrvtTestnetExitMarket(mark,previous.candles,history_start_t,end,previous.history_digest,timeframe)
        def kline(url: str,body: dict) -> dict:
            if url!='https://market-data.grvt.io/full/v1/kline':
                raise ValueError('unexpected public endpoint')
            current=clock_ns()
            if not at<=current<=mark.event_time_ns+5_000_000_000: raise ValueError('expired before page')
            reply=public_post(_ROOT+'/kline',body)
            data=reply.json() if reply.status_code==200 else None
            if not isinstance(data,dict) or 'error' in data or 'code' in data:
                raise ValueError('invalid testnet history')
            return data
        adapter=GrvtAdapter(http_post=kline)
        rows=adapter.get_historical_candles(symbol,timeframe,history_start_t,end,
            now=mark.event_time_ns//1_000_000_000)
        bars,gaps=validate_candles(rows,timeframe,now=end)
        checked=clock_ns()
        if gaps or type(checked) is not int or not at<=checked<=mark.event_time_ns+5_000_000_000:
            raise ValueError('expired or incomplete testnet history')
        digest=_encode({'source':'GRVT_TESTNET_TRADE_AND_MARK','symbol':symbol,'timeframe':timeframe,
            'start_t':history_start_t,'end_t':end,'candles':[bar.json() for bar in bars]})[1]
        return GrvtTestnetExitMarket(mark,tuple(bars),history_start_t,end,digest,timeframe)
    except Exception:
        raise GrvtStrategyExitError('Mercado de salida GRVT TESTNET no verificable') from None
