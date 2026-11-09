"""Decisión de salida desde fills TESTNET y mercado del mismo entorno.

No firma ni cancela órdenes. El coordinador debe comprobar después saldo,
posición residual, autorización y recibos. Un resultado CLOSE no es un fill.
"""

from dataclasses import dataclass
from decimal import Decimal,localcontext
from typing import Literal,Sequence

from app.lab.engine import (
    Candle,CURRENT_ENGINE_VERSION,INTERVALS,IndicatorCache,evaluate,
    paper_precision_for_engine,validate_candle_values,validate_strategy,
)
from app.live_execution.grvt_testnet_fills import GrvtTestnetFillAudit
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderObservation


from app.live_execution.grvt_exit_rules import (GrvtStrategyExitError,GrvtExitMark,GrvtFilledStrategyExit,
    required_exit_warmup,evaluate_verified_exit,_positive)


def evaluate_filled_strategy_exit(definition: dict,*,timeframe: str,engine_version: str,
        order: GrvtTestnetOrderObservation,audit: GrvtTestnetFillAudit,
        candles: Sequence[Candle],mark: GrvtExitMark,now_ns: int) -> GrvtFilledStrategyExit:
    """SL/TP sobre VWAP; trailing sólo con velas completas tras el último fill.

    El mark actual decide riesgo. Las reglas de cierre usan la última vela
    completa. La permanencia empieza en el último fill, no en la señal PAPER.
    No reconstruye operaciones durante periodos sin observación.
    """
    try:
        strategy=validate_strategy(definition)
        if engine_version!=CURRENT_ENGINE_VERSION or timeframe not in INTERVALS:
            raise ValueError('unsupported semantics')
        if (not isinstance(order,GrvtTestnetOrderObservation)
                or not isinstance(audit,GrvtTestnetFillAudit) or order.status!='FILLED'
                or order.side!=('BUY' if strategy['direction']=='LONG' else 'SELL')
                or order.client_order_id!=audit.client_order_id or order.order_id!=audit.order_id
                or audit.sizes_match is not True or not audit.fills
                or not _positive(audit.filled_size) or not _positive(audit.gross_notional)
                or audit.filled_size!=Decimal(order.size) or audit.filled_size!=Decimal(order.traded_size)):
            raise ValueError('entry not fully filled')
        with localcontext() as context:
            context.prec=100
            size=sum((row.size for row in audit.fills),Decimal(0))
            notional=sum((row.size*row.price for row in audit.fills),Decimal(0))
            last_fill=max(row.event_time_ns for row in audit.fills)
            if (any(not _positive(row.size) or not _positive(row.price) or row.realized_pnl!=0
                    or type(row.event_time_ns) is not int or row.event_time_ns<int(order.create_time_ns)
                    for row in audit.fills)
                    or len({row.trade_id for row in audit.fills})!=len(audit.fills)
                    or audit.fill_count!=len(audit.fills) or size!=audit.filled_size
                    or notional!=audit.gross_notional or last_fill!=audit.latest_fill_time_ns):
                raise ValueError('inconsistent entry evidence')
            return evaluate_verified_exit(definition,timeframe=timeframe,engine_version=engine_version,
                instrument=order.instrument,size=size,notional=notional,last_fill=last_fill,network='TESTNET',
                candles=candles,mark=mark,now_ns=now_ns)
    except Exception:
        raise GrvtStrategyExitError('Fills, estrategia o mercado de salida no verificables') from None
