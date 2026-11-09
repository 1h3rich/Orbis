"""Pure DCA decisions from native rules and actual DEX attempt facts.

This module has no wallet, repository, RPC or sender. Its caller must obtain
attempts from the verified journal and independently validate an exact DEX
execution before any decision becomes an authorized transaction.
"""
from dataclasses import dataclass,asdict
from datetime import datetime
from decimal import Decimal,InvalidOperation,localcontext
from types import MappingProxyType
from typing import Literal,Mapping,Any
from uuid import UUID

from app.automation.dca import (
    _period_key, adjusted_dca_request, dca_engine_for_rule, dca_trigger_candidates,
    dca_signal_window_key, dca_uses_average, live_dca_signal, validate_dca,
)
from app.lab.engine import Candle,validate_candles
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_plan import DexDecisionState,DexReferenceQuote
from app.live_execution.dex_bot_source import exact_units
from app.live_execution.dex_state import ExecutorState
from app.portfolio.wallet_funding import _time

ENGINE='orbis.dex.bot.dca.v1'
OUTCOME_ENGINE='orbis.dex.bot.dca.v2'
from app.live_execution.dex_versions import DCA_POLICY_OUTCOME as POLICY_OUTCOME_ENGINE
from app.live_execution.dex_versions import EXTERNAL_DCA_ENGINES
OUTCOME_ENGINES=(OUTCOME_ENGINE,POLICY_OUTCOME_ENGINE,*EXTERNAL_DCA_ENGINES)
ENGINES=(ENGINE,*OUTCOME_ENGINES)
DAY=86400
UNIT=10**18


@dataclass(frozen=True,slots=True)
class DexDcaLimits:
    entry_units:int
    total_budget_units:int
    monthly_limit_units:int
    activation_window_seconds:int=900

    def validate(self)->None:
        if any(type(value) is not int or not 0<value<2**128 for value in (self.entry_units,self.total_budget_units,self.monthly_limit_units)):
            raise DexExecutorError('Límites DCA REAL no válidos')
        if type(self.activation_window_seconds) is not int or not 60<=self.activation_window_seconds<=DAY:
            raise DexExecutorError('Ventana DCA: entre un minuto y un día')
        if self.entry_units>self.total_budget_units:raise DexExecutorError('La entrada supera el presupuesto DCA REAL')


@dataclass(frozen=True,slots=True)
class DexDcaCursor:
    last_candle_t:int
    last_checked_at:datetime
    reference:str


@dataclass(frozen=True,slots=True)
class DexDcaAttempt:
    id:str
    key:str
    kind:Literal['BASE','DIP']
    candle_t:int
    decided_at:datetime
    reference:str
    drop_pct:str|None
    amount_units:int
    status:Literal['PENDING','FINALIZED','REVERTED','ABANDONED_UNSENT','CANCELLED','EXTERNAL_REVIEWED']
    executed_at:datetime|None
    gas_fee_wei:int


@dataclass(frozen=True,slots=True)
class DexDcaProjection:
    total_spent_units:int
    monthly_spent_units:Mapping[str,int]
    gas_spent_wei:int
    consumed_keys:frozenset[str]
    triggered:frozenset[str]
    last_dip_candle_t:int|None
    pending:bool


@dataclass(frozen=True,slots=True)
class DexDcaPlan:
    action:Literal['WAIT','BUY','PAUSE']
    reason:str
    candle_t:int
    amount_units:int
    reference:str
    dca:dict[str,Any]|None
    evidence:dict[str,Any]

    def json(self)->dict[str,Any]:return {**asdict(self),'amount_units':str(self.amount_units)}


@dataclass(frozen=True,slots=True)
class DexDcaExecutionQuote:
    """Extra DCA checks; not an executable transaction or proof of a fill."""
    buy:bool
    amount_units:int
    quote_out_units:int
    deadline:int
    observed_at:datetime


def _price(value:str)->Decimal:
    if type(value) is not str or not 1<=len(value)<=100:raise ValueError('price format')
    number=Decimal(value)
    if not number.is_finite() or number<=0 or abs(number.as_tuple().exponent)>100:raise ValueError('price')
    return number


def fold_attempts(attempts:tuple[DexDcaAttempt,...],*,definition:dict,now:datetime,
                  observed_chain_at:datetime|None=None,engine:str=ENGINE)->DexDcaProjection:
    """Confirmed token spend uses execution UTC month; gas remains native BNB.

    A reserved key survives a reverted transaction and a later permission.
    Only the explicit ABANDONED_UNSENT proof releases a never-sent key.
    """
    try:
        if engine not in ENGINES:raise ValueError('DCA engine')
        rule=validate_dca(definition);now=_time(now)
        horizon=now if observed_chain_at is None else _time(observed_chain_at)
        if not 0<=(horizon-now).total_seconds()<=15:raise ValueError('chain clock horizon')
        if type(attempts) is not tuple:raise ValueError('attempt collection')
        identities=set();consumed=set();triggered=set();monthly={};spent=gas=0;last_dip=None;pending=False
        tiers={row['drop_pct'] for row in rule['dips']}
        for row in attempts:
            if type(row) is not DexDcaAttempt or str(UUID(row.id))!=row.id or row.id in identities:raise ValueError('attempt identity')
            identities.add(row.id);at=_time(row.decided_at)
            if (at>now or type(row.candle_t) is not int or row.candle_t%DAY or row.candle_t+DAY>at.timestamp()
                    or type(row.amount_units) is not int or not 0<row.amount_units<2**128
                    or type(row.gas_fee_wei) is not int or not 0<=row.gas_fee_wei<2**256):raise ValueError('attempt facts')
            _price(row.reference)
            if row.kind=='BASE':
                if row.drop_pct is not None or row.key!='BASE:'+_period_key(int(at.timestamp()),rule['frequency']):raise ValueError('base key')
            elif row.kind=='DIP':
                if row.drop_pct not in tiers or row.key!='DIP:'+row.drop_pct:raise ValueError('dip key')
            else:raise ValueError('activation kind')
            resolved=('FINALIZED','REVERTED','CANCELLED','EXTERNAL_REVIEWED')if engine in EXTERNAL_DCA_ENGINES else ('FINALIZED','REVERTED','CANCELLED')if engine in OUTCOME_ENGINES else ('FINALIZED','REVERTED')
            if row.status in resolved:
                executed=_time(row.executed_at)
                if executed>horizon or (executed-at).total_seconds()<-15 or row.gas_fee_wei<0 or row.gas_fee_wei==0 and row.status!='EXTERNAL_REVIEWED':raise ValueError('receipt time/gas')
                gas+=row.gas_fee_wei
                if row.status=='FINALIZED':
                    spent+=row.amount_units;month=executed.strftime('%Y-%m')
                    monthly[month]=monthly.get(month,0)+row.amount_units
            elif row.status in ('PENDING','ABANDONED_UNSENT'):
                if row.executed_at is not None or row.gas_fee_wei!=0:raise ValueError('unsettled receipt')
            else:raise ValueError('attempt status')
            if row.status=='ABANDONED_UNSENT':continue
            if row.key in consumed:raise ValueError('duplicate activation')
            consumed.add(row.key);pending=pending or row.status=='PENDING'
            if row.kind=='DIP':
                triggered.add(row.drop_pct);last_dip=max(last_dip or row.candle_t,row.candle_t)
        if len(triggered)>rule['max_extra_activations']:raise ValueError('extra activation ceiling')
        return DexDcaProjection(spent,MappingProxyType(monthly),gas,frozenset(consumed),frozenset(triggered),last_dip,pending)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,InvalidOperation):
        raise DexExecutorError('El historial DCA REAL no es coherente') from None


def plan_dca(*,definition:dict,candles:list[Candle],cursor:DexDcaCursor,snapshot:ExecutorState|DexDecisionState,
             quote:DexReferenceQuote,limits:DexDcaLimits,attempts:tuple[DexDcaAttempt,...],skip_through_t:int,now:datetime,engine:str=ENGINE)->DexDcaPlan:
    try:
        if engine not in ENGINES:raise ValueError('DCA engine')
        rule=validate_dca(definition);now=_time(now)
        if type(cursor) is not DexDcaCursor or type(limits) is not DexDcaLimits:raise ValueError('DCA scope')
        if type(snapshot) not in (ExecutorState,DexDecisionState):raise ValueError('executor observation')
        horizon=max(now,_time(snapshot.block_at))
        limits.validate();projection=fold_attempts(attempts,definition=rule,now=now,observed_chain_at=horizon,engine=engine)
        reference=_price(cursor.reference);checked=_time(cursor.last_checked_at)
        references=[_price(item.reference) for item in attempts]
        if (rule.get('reference_mode','initial')=='initial' and any(value!=reference for value in references)
                or rule.get('reference_mode','initial')=='high_watermark' and any(value>reference for value in references)):
            raise ValueError('reference continuity')
        if (type(skip_through_t) is not int or skip_through_t%DAY
                or type(cursor.last_candle_t) is not int or cursor.last_candle_t%DAY):raise ValueError('cursor')
        bars,gaps=validate_candles([bar.json() for bar in candles],'1d',now=int(now.timestamp()))
        if gaps or len(bars)<3:raise ValueError('closed daily history')
        if type(snapshot) not in (ExecutorState,DexDecisionState) or type(quote) is not DexReferenceQuote:raise ValueError('market observation')
        for value in (snapshot.owned_wbnb,snapshot.cost_usdt,snapshot.buy_spent,snapshot.entries,snapshot.realized_loss,
                      snapshot.acquired_wbnb,snapshot.balance_usdt,snapshot.balance_wbnb,snapshot.last_entry_at):
            if type(value) is not int or not 0<=value<2**256:raise ValueError('executor units')
        if (snapshot.owned_wbnb==0)!=(snapshot.cost_usdt==0) or snapshot.acquired_wbnb<snapshot.owned_wbnb:raise ValueError('position')
        bid,ask=_price(str(quote.bid)),_price(str(quote.ask))
        if (type(quote.bid) is not Decimal or type(quote.ask) is not Decimal or ask<=bid
                or quote.symbol!='BNB/USDT' or quote.source!='mexc-spot-public'):raise ValueError('reference market')
        observed,event=_time(quote.observed_at),_time(quote.event_at)
        monthly_cap=min(limits.monthly_limit_units,exact_units(rule['monthly_limit'],'Límite mensual DCA',maximum=10**30))
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,OverflowError,InvalidOperation):
        raise DexExecutorError('Reglas, histórico o límites DCA no válidos') from None
    evidence={'engine':engine,'source_engine':dca_engine_for_rule(rule),'reference_source':quote.source,'reference_symbol':quote.symbol,
        'execution_market':'Uniswap v3 · USDT/WBNB · BNB Chain',
        'spent_units':str(projection.total_spent_units),'monthly_spent_units':str(projection.monthly_spent_units.get(now.strftime('%Y-%m'),0)),
        'gas_spent_wei':str(projection.gas_spent_wei),'activation_window_seconds':limits.activation_window_seconds}
    def result(action,reason,amount=0,dca=None):return DexDcaPlan(action,reason,bars[-1].t,amount,str(reference),dca,evidence)
    if not 0<=(now-checked).total_seconds()<=30:return result('PAUSE','OBSERVATION_GAP')
    if not 0<=(now-observed).total_seconds()<=15 or not 0<=(observed-event).total_seconds()<=15:
        return result('PAUSE','REFERENCE_QUOTE_STALE')
    due=int(now.timestamp())//DAY*DAY-DAY
    if bars[-1].t<cursor.last_candle_t or bars[-1].t>cursor.last_candle_t+DAY:return result('PAUSE','CANDLE_CONTINUITY_LOST')
    pending_bar=bars[-1].t+DAY==due and 0<=now.timestamp()-(due+DAY)<=15
    if bars[-1].t!=due and not pending_bar:return result('PAUSE','CANDLE_CONTINUITY_LOST')
    try:snapshot.require_fresh(now)
    except DexExecutorError:return result('PAUSE','EXECUTOR_STATE_STALE')
    if horizon.strftime('%Y-%m')!=now.strftime('%Y-%m'):return result('WAIT','DCA_PERIOD_BOUNDARY_SKEW')
    policy=snapshot.policy_limits
    if policy is None or not snapshot.current_operator_code_empty or now.timestamp()>=policy.expires_at:
        return result('PAUSE','POLICY_UNAVAILABLE')
    if not snapshot.position_covered:return result('PAUSE','POSITION_NOT_COVERED')
    if projection.pending:return result('WAIT','ORDER_PENDING')
    with localcontext() as context:
        context.prec=300
        if rule.get('reference_mode','initial')=='high_watermark':reference=max(reference,bars[-1].c)
        if bars[-1].t<=skip_through_t:return result('WAIT','DCA_WAITING_NEXT_CANDLE')
        if pending_bar:return result('WAIT','WAITING_CANDLE')
        if not 0<=now.timestamp()-(bars[-1].t+DAY)<limits.activation_window_seconds:return result('WAIT','DCA_SIGNAL_EXPIRED')
        if not policy.allow_buy:return result('PAUSE','BUY_DISABLED')
        if policy.allow_sell:return result('PAUSE','DCA_POLICY_NOT_BUY_ONLY')
        if snapshot.entries>=policy.max_entries:return result('WAIT','ENTRY_LIMIT')
        if snapshot.acquired_wbnb>=policy.max_wbnb_acquired:return result('WAIT','WBNB_QUOTA_EXHAUSTED')
        if snapshot.realized_loss>=policy.max_realized_loss:return result('WAIT','LOSS_LIMIT')
        if snapshot.last_entry_at and now.timestamp()<snapshot.last_entry_at+policy.cooldown_seconds:return result('WAIT','ENTRY_COOLDOWN')
        spent=projection.total_spent_units;month_spent=projection.monthly_spent_units.get(now.strftime('%Y-%m'),0)
        if spent>limits.total_budget_units or month_spent>monthly_cap:return result('PAUSE','DCA_EXECUTED_BUDGET_EXCEEDED')
        cash=limits.total_budget_units-spent;month_room=monthly_cap-month_spent
        evidence.update(total_remaining_units=str(cash),monthly_remaining_units=str(month_room))
        period=_period_key(int(now.timestamp()),rule['frequency'])
        candidates=dca_trigger_candidates(rule,signal_close=bars[-1].c,reference=reference,period=period,
            last_period=period if 'BASE:'+period in projection.consumed_keys else None,triggered=set(projection.triggered),
            extra_count=len(projection.triggered),index=bars[-1].t//DAY,
            last_extra_index=projection.last_dip_candle_t//DAY if projection.last_dip_candle_t is not None else None,
            allow_average=True,allow_volume=True)
        factor=Decimal(1)
        average=dca_uses_average(rule)
        if candidates and dca_signal_window_key(rule):
            factor,rejection,signal_evidence=live_dca_signal(rule,bars)
            evidence['average' if average else 'volume']=signal_evidence
            if rejection is not None:return result('WAIT',rejection)
        # This is a mark at the current reference, not a DEX fill prediction.
        # The journal's execution stage must also verify its pinned DEX quote.
        marked=Decimal(snapshot.owned_wbnb)*bid;fraction=Decimal(rule['max_exposure']);ratio=bid/ask
        room=max(Decimal(0),fraction*(Decimal(cash)+marked)-marked)
        exposure_room=int(room/(ratio+fraction*(1-ratio)))
        reason='DCA_BUDGET_OR_EXPOSURE' if candidates else 'DCA_WAITING_TRIGGER'
        for kind,requested,signal in candidates:
            key='BASE:'+period if kind=='BASE' else 'DIP:'+signal['drop_pct']
            if key in projection.consumed_keys:continue
            native=exact_units(str(requested),'Compra DCA',maximum=10**30)
            if average:
                adjusted=adjusted_dca_request(requested,factor)
                # A calculated amount may contain fractional token units. Never
                # round up or change the precision of user-approved limits.
                native=int(adjusted*UNIT)
                if native==0:
                    reason='DCA_AMOUNT_BELOW_UNIT'
                    continue
                signal.update(base_requested=str(requested),requested_quote=str(adjusted))
            amount=min(native,limits.entry_units,cash,month_room,policy.max_buy_per_order,
                policy.buy_budget-snapshot.buy_spent,policy.max_exposure-snapshot.cost_usdt,snapshot.balance_usdt,exposure_room)
            if amount<=0:continue
            dca={'kind':kind,'key':key,'period':period,'requested_units':str(native),'reference':str(reference),**signal}
            return result('BUY','DCA_'+kind,amount,dca)
        return result('WAIT',reason)


def _next_month(now:datetime)->datetime:
    return now.replace(year=now.year+1 if now.month==12 else now.year,
        month=1 if now.month==12 else now.month+1,day=1,hour=0,minute=0,second=0,microsecond=0)


def execution_ttl(plan:DexDcaPlan,*,now:datetime,policy_expires_at:int,maximum_seconds:int=60)->int:
    """Never request an executor deadline beyond the DCA window or UTC month."""
    now=_time(now)
    try:
        if (type(plan) is not DexDcaPlan or plan.action!='BUY' or plan.evidence['engine'] not in ENGINES
                or type(policy_expires_at) is not int or not 0<policy_expires_at<2**64
                or type(maximum_seconds) is not int or not 1<=maximum_seconds<=120):raise ValueError('deadline scope')
        window=plan.evidence['activation_window_seconds']
        if type(window) is not int or not 60<=window<=DAY:raise ValueError('activation window')
        timestamp=int(now.timestamp())
        deadline=min(timestamp+maximum_seconds,policy_expires_at,plan.candle_t+DAY+window,int(_next_month(now).timestamp())-1)
        if deadline<=now.timestamp():raise ValueError('expired deadline')
        return deadline-timestamp
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('La ventana DCA ya no permite una ejecución vigente') from None


def validate_execution_quote(execution:DexDcaExecutionQuote,**planning)->DexDcaPlan:
    """Recheck the decision with the simulated DEX output before authorization.

    Its caller still validates the full executor recipe/signature/receipt.
    Reference prices and simulated output never become financial fills here.
    """
    plan=plan_dca(**planning)
    try:
        now=_time(planning['now']);snapshot=planning['snapshot'];limits=planning['limits']
        if (type(execution) is not DexDcaExecutionQuote or execution.buy is not True or plan.action!='BUY'
                or type(execution.amount_units) is not int or execution.amount_units!=plan.amount_units
                or type(execution.quote_out_units) is not int or not 0<execution.quote_out_units<2**256
                or type(execution.deadline) is not int or not 0<execution.deadline<2**64
                or _time(execution.observed_at)!=_time(snapshot.observed_at)
                or not 0<=(now-_time(execution.observed_at)).total_seconds()<=15):
            raise ValueError('execution scope')
        next_month=_next_month(now)
        if (not int(now.timestamp())<execution.deadline<next_month.timestamp()
                or execution.deadline>snapshot.policy_limits.expires_at
                or execution.deadline>int(execution.observed_at.timestamp())+120
                or execution.deadline>plan.candle_t+DAY+limits.activation_window_seconds
                or snapshot.acquired_wbnb+execution.quote_out_units>snapshot.policy_limits.max_wbnb_acquired):
            raise ValueError('quote deadline/quota')
        projection=fold_attempts(planning['attempts'],definition=planning['definition'],now=now,
            observed_chain_at=max(now,_time(snapshot.block_at)),engine=planning.get('engine',ENGINE))
        with localcontext() as context:
            context.prec=300
            bid=_price(str(planning['quote'].bid));fraction=Decimal(validate_dca(planning['definition'])['max_exposure'])
            marked=Decimal(snapshot.owned_wbnb+execution.quote_out_units)*bid
            cash=limits.total_budget_units-projection.total_spent_units-execution.amount_units
            if cash<0 or marked>fraction*(Decimal(cash)+marked):raise ValueError('quoted exposure')
        return plan
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,InvalidOperation):
        raise DexExecutorError('La cotización DEX no conserva la decisión y límites DCA') from None
