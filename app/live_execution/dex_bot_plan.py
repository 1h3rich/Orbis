"""Pure LONG decisions: LAB rules, MEXC reference, actual DEX position/cost.

Reference prices trigger a decision; only a separately simulated executor plan
may become a fill. No PAPER cash, units, allocation or entry price is imported.
"""
from dataclasses import dataclass,asdict
from datetime import datetime
from decimal import Decimal,InvalidOperation,localcontext
from typing import Any,Literal

from app.lab.engine import Candle,IndicatorCache,INTERVALS,evaluate,validate_candles,validate_strategy
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_state import ExecutorState
from app.live_execution.dex_policy import PolicyLimits
from app.portfolio.wallet_funding import _time

UNIT=10**18


@dataclass(frozen=True,slots=True)
class DexDecisionState:
    """Business observation for deterministic replay, without RPC identities."""
    owned_wbnb:int
    cost_usdt:int
    buy_spent:int
    entries:int
    realized_loss:int
    acquired_wbnb:int
    balance_usdt:int
    balance_wbnb:int
    last_entry_at:int
    current_operator_code_empty:bool
    policy_limits:PolicyLimits|None
    block_at:datetime
    observed_at:datetime

    @classmethod
    def from_executor(cls,state:ExecutorState)->'DexDecisionState':
        if type(state) is not ExecutorState:raise DexExecutorError('Observación de ejecutor no válida')
        return cls(**{name:getattr(state,name) for name in cls.__dataclass_fields__})

    @property
    def position_covered(self)->bool:return self.balance_wbnb>=self.owned_wbnb

    def require_fresh(self,now:datetime)->None:
        if not -15<=(now-self.block_at).total_seconds()<=30 or not 0<=(now-self.observed_at).total_seconds()<=30:
            raise DexExecutorError('Observación de decisión caducada')


@dataclass(frozen=True,slots=True)
class DexReferenceQuote:
    bid:Decimal
    ask:Decimal
    event_at:datetime
    observed_at:datetime
    symbol:str='BNB/USDT'
    source:str='mexc-spot-public'

    def json(self)->dict[str,str]:
        return {'bid':str(self.bid),'ask':str(self.ask),'event_at':self.event_at.isoformat(),
            'observed_at':self.observed_at.isoformat(),'symbol':self.symbol,'source':self.source}


@dataclass(frozen=True,slots=True)
class DexBotCursor:
    last_candle_t:int
    last_checked_at:datetime
    entry_candle_t:int|None
    trailing_high:str|None
    closing_reason:str|None


@dataclass(frozen=True,slots=True)
class DexBotPlan:
    action:Literal['WAIT','BUY','SELL','PAUSE']
    reason:str
    candle_t:int
    amount_units:int
    trailing_high:str|None
    closing_reason:str|None
    evidence:dict[str,Any]

    def json(self)->dict[str,Any]:
        return {**asdict(self),'amount_units':str(self.amount_units)}


EXIT_REASONS=frozenset({'STOP_LOSS','TRAILING_STOP','TAKE_PROFIT','EXIT_RULE','TIME_LIMIT'})


def plan_strategy(*,definition:dict,timeframe:str,candles:list[Candle],cursor:DexBotCursor,snapshot:ExecutorState|DexDecisionState,
                  quote:DexReferenceQuote,entry_units:int,exit_cap_units:int,now:datetime)->DexBotPlan:
    now=_time(now);definition=validate_strategy(definition)
    if (definition['direction']!='LONG' or timeframe not in INTERVALS or type(snapshot) not in (ExecutorState,DexDecisionState)
            or type(cursor) is not DexBotCursor or type(quote) is not DexReferenceQuote
            or type(entry_units) is not int or not 0<entry_units<2**128
            or type(exit_cap_units) is not int or not 0<=exit_cap_units<2**128):
        raise DexExecutorError('Ámbito o importes de decisión DEX no válidos')
    step=INTERVALS[timeframe]
    try:
        bars,gaps=validate_candles([bar.json() for bar in candles],timeframe,now=int(now.timestamp()))
        if gaps or len(bars)<3 or bars[-1].t+step>int(now.timestamp()):raise ValueError('closed bars')
        if (type(cursor.last_candle_t) is not int or cursor.last_candle_t%step
                or cursor.entry_candle_t is not None and (type(cursor.entry_candle_t) is not int or cursor.entry_candle_t%step)
                or cursor.closing_reason is not None and cursor.closing_reason not in EXIT_REASONS):raise ValueError('cursor')
        if cursor.trailing_high is not None:
            if type(cursor.trailing_high) is not str or len(cursor.trailing_high)>100:raise ValueError('trailing price')
            peak=Decimal(cursor.trailing_high)
            if not peak.is_finite() or peak<=0 or abs(peak.as_tuple().exponent)>100:raise ValueError('trailing price')
        checked=_time(cursor.last_checked_at);event=_time(quote.event_at);observed=_time(quote.observed_at)
        for value in (quote.bid,quote.ask):
            if (type(value) is not Decimal or not value.is_finite() or value<=0
                    or len(value.as_tuple().digits)>100 or abs(value.as_tuple().exponent)>100):raise ValueError('quote price')
        if quote.symbol!='BNB/USDT' or quote.source!='mexc-spot-public' or quote.ask<=quote.bid:raise ValueError('reference market')
        for value in (snapshot.owned_wbnb,snapshot.cost_usdt,snapshot.buy_spent,snapshot.entries,
                      snapshot.realized_loss,snapshot.acquired_wbnb,snapshot.balance_usdt,snapshot.balance_wbnb,snapshot.last_entry_at):
            if type(value) is not int or not 0<=value<2**256:raise ValueError('position units')
        if (snapshot.owned_wbnb==0)!=(snapshot.cost_usdt==0) or snapshot.acquired_wbnb<snapshot.owned_wbnb:raise ValueError('position cost')
    except (ValueError,TypeError,AttributeError,KeyError,IndexError,OverflowError,InvalidOperation):
        raise DexExecutorError('Velas, cursor, referencia o posición DEX no válidos') from None
    # Evaluate the full rules once with the exact LAB precision and completed
    # higher-timeframe aggregation, not a reduced two-candle approximation.
    indicators=IndicatorCache(bars)
    entry,entry_detail=evaluate(definition['entry'],bars,len(bars)-1,indicators,base_timeframe=timeframe)
    exit_match,exit_detail=(evaluate(definition['exit'],bars,len(bars)-1,indicators,base_timeframe=timeframe)
        if 'exit' in definition else (None,None))
    evidence={'engine':'lab-1.16','entry_match':entry,'entry':entry_detail,'exit_match':exit_match,'exit':exit_detail,
        'reference_source':quote.source,'reference_symbol':quote.symbol,'execution_market':'Uniswap v3 · USDT/WBNB · BNB Chain'}
    high,closing=cursor.trailing_high,cursor.closing_reason
    def result(action,reason,amount=0):
        return DexBotPlan(action,reason,bars[-1].t,amount,high,closing,evidence)
    if not 0<=(now-checked).total_seconds()<=30:return result('PAUSE','OBSERVATION_GAP')
    if not 0<=(now-observed).total_seconds()<=15 or not 0<=(observed-event).total_seconds()<=15:
        return result('PAUSE','REFERENCE_QUOTE_STALE')
    due=int(now.timestamp())//step*step-step
    if bars[-1].t<cursor.last_candle_t or bars[-1].t>cursor.last_candle_t+step:return result('PAUSE','CANDLE_CONTINUITY_LOST')
    pending=bars[-1].t+step==due and 0<=now.timestamp()-(due+step)<=15
    if bars[-1].t!=due and not pending:return result('PAUSE','CANDLE_CONTINUITY_LOST')
    if pending:evidence['candle_pending']=True
    try:snapshot.require_fresh(now)
    except DexExecutorError:return result('PAUSE','EXECUTOR_STATE_STALE')
    limits=snapshot.policy_limits
    if limits is None or not snapshot.current_operator_code_empty or now.timestamp()>=limits.expires_at:
        return result('PAUSE','POLICY_UNAVAILABLE')
    if not snapshot.position_covered:return result('PAUSE','POSITION_NOT_COVERED')
    new=bars[-1].t>cursor.last_candle_t
    with localcontext() as context:
        context.prec=300
        if snapshot.owned_wbnb:
            if cursor.entry_candle_t is None or cursor.entry_candle_t>bars[-1].t:return result('PAUSE','POSITION_PROVENANCE_MISSING')
            average=Decimal(snapshot.cost_usdt)/Decimal(snapshot.owned_wbnb)
            peak=Decimal(high) if high is not None else average
            if not peak.is_finite() or peak<average:return result('PAUSE','TRAILING_STATE_INVALID')
            high=str(max(peak,average,quote.bid))
            reason=closing
            if reason is None and quote.bid<=average*(1-Decimal(definition['stop_loss'])):reason='STOP_LOSS'
            if reason is None and 'trailing_stop' in definition and quote.bid<=peak*(1-Decimal(definition['trailing_stop'])):reason='TRAILING_STOP'
            if reason is None and quote.bid>=average*(1+Decimal(definition['take_profit'])):reason='TAKE_PROFIT'
            if reason is None and new and exit_match:reason='EXIT_RULE'
            if (reason is None and new and 'max_holding_bars' in definition
                    and bars[-1].t-cursor.entry_candle_t>=definition['max_holding_bars']*step):reason='TIME_LIMIT'
            if reason is None:return result('WAIT','POSITION_OPEN')
            closing=reason
            if not limits.allow_sell:return result('PAUSE','SELL_DISABLED')
            # The caller obtains this cap from the DEX quote. The subsequent
            # executor-plan validator still enforces the on-chain notional cap.
            amount=min(snapshot.owned_wbnb,exit_cap_units)
            return result('SELL',reason,amount) if amount else result('PAUSE','EXIT_QUOTE_UNAVAILABLE')
        high=closing=None
        if pending:return result('WAIT','WAITING_CANDLE')
        if not new or not entry:return result('WAIT','WAITING_ENTRY')
        if not 0<=now.timestamp()-(bars[-1].t+step)<=15:return result('WAIT','ENTRY_SIGNAL_EXPIRED')
        if not limits.allow_buy:return result('PAUSE','BUY_DISABLED')
        if snapshot.entries>=limits.max_entries:return result('WAIT','ENTRY_LIMIT')
        if snapshot.acquired_wbnb>=limits.max_wbnb_acquired:return result('WAIT','WBNB_QUOTA_EXHAUSTED')
        if snapshot.realized_loss>=limits.max_realized_loss:return result('WAIT','LOSS_LIMIT')
        if snapshot.last_entry_at and now.timestamp()<snapshot.last_entry_at+limits.cooldown_seconds:return result('WAIT','ENTRY_COOLDOWN')
        amount=min(entry_units,limits.max_buy_per_order,limits.buy_budget-snapshot.buy_spent,
            limits.max_exposure-snapshot.cost_usdt,snapshot.balance_usdt)
        return result('BUY','ENTRY_RULE',amount) if amount>0 else result('WAIT','ENTRY_BUDGET_EXHAUSTED')
