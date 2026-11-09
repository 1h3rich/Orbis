"""Reglas de salida comunes sobre fills ya verificados. No autoriza ejecución."""
from dataclasses import dataclass
from decimal import Decimal,localcontext
from typing import Literal,Sequence
from app.lab.engine import (Candle,CURRENT_ENGINE_VERSION,INTERVALS,IndicatorCache,evaluate,
    paper_precision_for_engine,validate_candle_values,validate_strategy)

class GrvtStrategyExitError(ValueError):
    """No se puede deducir una salida con la evidencia proporcionada."""


@dataclass(frozen=True,slots=True)
class GrvtExitMark:
    instrument: str
    event_time_ns: int
    observed_at_ns: int
    price: Decimal


ExitReason=Literal['STOP','TRAILING_STOP','TARGET','EXIT_RULE','TIME_LIMIT','POSITION_OPEN']


@dataclass(frozen=True,slots=True)
class GrvtFilledStrategyExit:
    action: Literal['HOLD','CLOSE']
    reason: ExitReason
    instrument: str
    direction: Literal['LONG','SHORT']
    size: Decimal
    entry_price: Decimal
    entry_notional: Decimal
    last_fill_at_ns: int
    stop_price: Decimal
    target_price: Decimal
    trailing_extreme: Decimal|None
    mark_price: Decimal
    market_event_time_ns: int
    last_closed_candle_t: int
    time_limit_at_ns: int|None
    exit_rule_evidence: dict|None
    trigger_basis: Literal['TESTNET_MARK','MAINNET_MARK']='TESTNET_MARK'
    execution_authorized: Literal[False]=False


def _positive(value: object) -> bool:
    return type(value) is Decimal and value.is_finite() and value>0


def required_exit_warmup(rule: dict,step: int) -> int:
    """Prefijo conservador para indicadores/reglas; no inventa valores iniciales."""
    if 'op' in rule: return max(required_exit_warmup(child,step) for child in rule['children'])
    count=max(rule['period'],rule.get('slow_period',0))+rule.get('signal_period',0)+1
    return count*(INTERVALS[rule['timeframe']]//step if 'timeframe' in rule else 1)


def _ready(detail: dict) -> bool:
    if 'children' in detail: return all(_ready(child) for child in detail['children'])
    return (not detail.get('reason') and all(value is not None for key,value in detail.items()
        if key in ('observed','threshold','previous_observed','previous_threshold','higher_candle_t')))


def evaluate_verified_exit(definition: dict,*,timeframe: str,engine_version: str,
        instrument: str,size: Decimal,notional: Decimal,last_fill: int,network: Literal['MAINNET','TESTNET'],
        candles: Sequence[Candle],mark: GrvtExitMark,now_ns: int) -> GrvtFilledStrategyExit:
    try:
        strategy=validate_strategy(definition)
        if (engine_version!=CURRENT_ENGINE_VERSION or timeframe not in INTERVALS or network not in ('MAINNET','TESTNET')
                or not isinstance(instrument,str) or not _positive(size) or not _positive(notional)
                or type(last_fill) is not int or last_fill<=0):raise ValueError('unverified entry scope')
        with localcontext() as context:
            context.prec=100
            if (not isinstance(mark,GrvtExitMark) or mark.instrument!=instrument
                    or not _positive(mark.price) or type(now_ns) is not int
                    or type(mark.event_time_ns) is not int or type(mark.observed_at_ns) is not int
                    or not 0<last_fill<=mark.event_time_ns<=mark.observed_at_ns<=now_ns
                    or now_ns-mark.event_time_ns>5_000_000_000):
                raise ValueError('stale or unrelated market')
            step=INTERVALS[timeframe]
            bars=list(candles)
            validate_candle_values(bars,timeframe)
            last_closed=(mark.event_time_ns//1_000_000_000//step)*step-step
            entry_bucket=last_fill//1_000_000_000//step*step
            if (len(bars)<3 or len(bars)>5000 or bars[-1].t!=last_closed
                    or bars[0].t>min(entry_bucket,last_closed)
                    or any(b.t-a.t!=step for a,b in zip(bars,bars[1:]))):
                raise ValueError('incomplete closed market history')
            entry=notional/size
            sign=1 if strategy['direction']=='LONG' else -1
            fixed=entry*(1-sign*Decimal(strategy['stop_loss']))
            target=entry*(1+sign*Decimal(strategy['take_profit']))
            if target<=0: raise ValueError('unreachable short target')
            extreme=None; stop=fixed; stop_reason: ExitReason='STOP'
            if 'trailing_stop' in strategy:
                extreme=entry
                for bar in bars:
                    if bar.t<=entry_bucket: continue
                    extreme=max(extreme,bar.h) if sign==1 else min(extreme,bar.l)
                trailing=extreme*(1-sign*Decimal(strategy['trailing_stop']))
                if (trailing>fixed if sign==1 else trailing<fixed):
                    stop=trailing; stop_reason='TRAILING_STOP'
            reason: ExitReason='POSITION_OPEN'; rule_evidence=None
            if (mark.price<=stop if sign==1 else mark.price>=stop): reason=stop_reason
            elif (mark.price>=target if sign==1 else mark.price<=target): reason='TARGET'
            elif 'exit' in strategy and bars[-1].t>entry_bucket:
                cache=IndicatorCache(bars,precision=paper_precision_for_engine(engine_version))
                matched,rule_evidence=evaluate(strategy['exit'],bars,len(bars)-1,cache,base_timeframe=timeframe)
                if len(bars)<required_exit_warmup(strategy['exit'],step) or not _ready(rule_evidence):
                    raise ValueError('insufficient exit rule history')
                if matched: reason='EXIT_RULE'
            limit=(last_fill+strategy['max_holding_bars']*step*1_000_000_000
                if 'max_holding_bars' in strategy else None)
            if reason=='POSITION_OPEN' and limit is not None and mark.event_time_ns>=limit:
                reason='TIME_LIMIT'
            return GrvtFilledStrategyExit('HOLD' if reason=='POSITION_OPEN' else 'CLOSE',reason,
                instrument,strategy['direction'],size,entry,notional,last_fill,stop,target,extreme,
                mark.price,mark.event_time_ns,bars[-1].t,limit,rule_evidence,network+'_MARK')
    except Exception:
        raise GrvtStrategyExitError('Fills, estrategia o mercado de salida no verificables') from None
