"""Observa snapshots públicos TESTNET; sólo el diario puede autorizar un cierre.

La firma/credenciales pertenecen al servicio que lo invoca. No se guardan aquí.
No interpreta un hueco como una operación reconstruida ni reabre posiciones.
"""

from collections.abc import Callable
from dataclasses import asdict

from app.derivatives.paper_math import PerpQuote
from app.lab.engine import validate_candles
from app.live_execution.grvt_bracket_exit_review import collect_bracket_exit_evidence
from app.live_execution.grvt_bracket_strategy_snapshot import read_bracket_strategy
from app.live_execution.grvt_filled_close import _ArchivedFills,request_filled_strategy_close,FilledCloseRequest
from app.live_execution.grvt_filled_strategy_exit import GrvtExitMark,evaluate_filled_strategy_exit
from app.live_execution.grvt_testnet_exit_market import GrvtTestnetExitMarket,fetch_testnet_exit_market
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,GrvtTestnetOrderObservation
from app.live_execution.grvt_native_exit import (NativeGroupExit, NativePartialExit, NativeRecoveryExit, NativeExitObserved,
    NATIVE_CHECK_INTERVAL_NS, inspect_native_exit)
from app.portfolio.vault import RealVault


class ExitWatchGap(RuntimeError): pass


def watch_filled_exit(vault: RealVault,client: GrvtTestnetOrderClient,parent: str,*,max_attempts: int,
        deadline_ns: int,stream_factory: Callable,clock_ns: Callable[[],int],public_post: Callable,
        on_observation: Callable[[dict],None]) -> FilledCloseRequest | NativeGroupExit | NativePartialExit | NativeRecoveryExit:
    snapshot=read_bracket_strategy(vault,parent)
    if snapshot is None: raise ExitWatchGap('Falta definición archivada')
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    plan=read_bracket(vault,parent); symbol=plan['instrument'].replace('_USDT_Perp','/USDT:PERP')
    native=inspect_native_exit(vault,client,parent,clock_ns=clock_ns)
    if native is not None: return native
    last_native_check=clock_ns()
    market=None; order=None; audit=None; previous=None
    with stream_factory(symbol,clock_ns) as stream:
        last_received=clock_ns()
        while True:
            now=clock_ns()
            if now-last_received>5_000_000_000: raise ExitWatchGap('Intervalo sin observación TESTNET')
            try: incoming=stream.recv(timeout=1)
            except TimeoutError: continue
            if incoming is None: continue
            name,quote=incoming; now=clock_ns()
            if (name!=symbol or not isinstance(quote,PerpQuote) or quote.instrument!=plan['instrument']
                    or not 0<=now-quote.event_time_ns<=5_000_000_000):
                raise ExitWatchGap('Ticker TESTNET no verificable')
            if previous is not None:
                old_time,old_price=previous
                if (quote.event_time_ns<old_time or quote.event_time_ns-old_time>5_000_000_000
                        or quote.event_time_ns==old_time and quote.mark!=old_price):
                    raise ExitWatchGap('Ticker TESTNET discontinuo o contradictorio')
                if quote.event_time_ns==old_time: continue
            previous=(quote.event_time_ns,quote.mark); last_received=now
            if now-last_native_check >= NATIVE_CHECK_INTERVAL_NS:
                native=inspect_native_exit(vault,client,parent,clock_ns=clock_ns)
                if native is not None: return native
                last_native_check=clock_ns()
            mark=GrvtExitMark(quote.instrument,quote.event_time_ns,now,quote.mark)
            if market is None:
                try:
                    _,evidence=collect_bracket_exit_evidence(vault,client,parent,public_post=public_post,
                        clock_ns=clock_ns,mark=mark)
                except NativeExitObserved as found:
                    return found.outcome
                raw=evidence['market']; history=raw['history']
                bars,_=validate_candles(history['candles'],snapshot['timeframe'],now=history['end_t'])
                market=GrvtTestnetExitMarket(mark,tuple(bars),history['start_t'],history['end_t'],
                    raw['history_digest'],snapshot['timeframe'])
                order=GrvtTestnetOrderObservation(**evidence['entry_order'])
                audit=audit_testnet_fills(_ArchivedFills(evidence['fills']),order,now_ns=evidence['evaluated_at_ns'])
                decision=evidence['decision']
            else:
                market=fetch_testnet_exit_market(symbol,snapshot['timeframe'],history_start_t=snapshot['history_start_t'],
                    public_post=public_post,clock_ns=clock_ns,mark=mark,previous=market)
                candidate=evaluate_filled_strategy_exit(snapshot['definition'],timeframe=snapshot['timeframe'],
                    engine_version=snapshot['engine_version'],order=order,audit=audit,candles=market.candles,
                    mark=mark,now_ns=clock_ns())
                decision=asdict(candidate); evidence=None
            on_observation({'event_time_ns':quote.event_time_ns,'mark_price':format(quote.mark,'f'),
                'reason':decision['reason'],'action':decision['action']})
            if decision['action']!='CLOSE': continue
            if evidence is None:
                try:
                    _,evidence=collect_bracket_exit_evidence(vault,client,parent,public_post=public_post,
                        clock_ns=clock_ns,mark=mark,market_cache=market)
                except NativeExitObserved as found:
                    return found.outcome
            # La revalidación de IDs/fills precede al registro atómico de la
            # decisión; conservar el plazo original nunca lo amplía por observar.
            return request_filled_strategy_close(vault,client,parent,max_attempts=max_attempts,
                deadline_ns=min(deadline_ns,clock_ns()+300_000_000_000),public_post=public_post,
                clock_ns=clock_ns,prepared_evidence=evidence)
