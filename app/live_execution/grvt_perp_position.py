"""Fresh position/configuration evidence for a supervised perpetual bot.

Only explicitly returned fields are used. Account-level margin never supplies
a missing position mode, and old position event times are not receipt times.
The result proposes a safety action; it has no execution authority.
"""
from __future__ import annotations

import time
from typing import Literal

from pydantic import Field,model_validator

from app.derivatives.perp_observed_wire import ExactInput,Nanoseconds
from app.integrations.grvt_position_config import PositionConfiguration,leverage
from app.lab.perp_book_types import exact
from app.live_execution.grvt_mainnet_preflight import number
from app.live_execution.grvt_perp_plan import FRESH_NS,PerpEntryTerms


class PerpPositionInput(ExactInput):
    sub_account_id: str=Field(pattern=r'^[1-9][0-9]{0,19}$')
    instrument: str=Field(pattern=r'^[A-Za-z0-9_:\-]{3,100}$')
    size: str
    event_time_ns: Nanoseconds|None
    margin_type: Literal['CROSS','ISOLATED']|None
    leverage: str|None
    isolated_balance: str|None
    isolated_im: str|None
    isolated_mm: str|None

    @model_validator(mode='after')
    def numbers(self):
        if int(self.sub_account_id)>=2**64:raise ValueError('Subcuenta de posición fuera de rango')
        number(self.size)
        if self.event_time_ns is not None and int(self.event_time_ns)>=2**63:
            raise ValueError('Hora de posición fuera de rango')
        if self.leverage is not None:leverage(self.leverage)
        for value in (self.isolated_balance,self.isolated_im,self.isolated_mm):
            if value is not None:number(value)
        return self


class PerpBalanceInput(ExactInput):
    currency: str=Field(pattern=r'^[A-Z0-9]{2,24}$')
    balance: str

    @model_validator(mode='after')
    def numeric(self):
        number(self.balance)
        return self


class PerpAccountPositionInput(ExactInput):
    sub_account_id: str=Field(pattern=r'^[1-9][0-9]{0,19}$')
    requested_at_ns: Nanoseconds
    received_at_ns: Nanoseconds
    event_time_ns: Nanoseconds
    settle_currency: str=Field(pattern=r'^[A-Z0-9]{2,24}$')
    margin_type: str=Field(pattern=r'^[A-Z][A-Z0-9_]{0,50}$')
    sub_account_mode: str=Field(pattern=r'^[A-Z][A-Z0-9_]{0,50}$')
    total_equity: str
    balances: list[PerpBalanceInput]=Field(min_length=1,max_length=100)
    positions: list[PerpPositionInput]=Field(max_length=10000)

    @model_validator(mode='after')
    def coherent(self):
        if int(self.sub_account_id)>=2**64:raise ValueError('Subcuenta de cuenta fuera de rango')
        number(self.total_equity)
        if any(int(at)>=2**63 for at in (self.requested_at_ns,self.received_at_ns,self.event_time_ns)):
            raise ValueError('Hora de cuenta fuera de rango')
        currencies=[row.currency for row in self.balances]
        if len(set(currencies))!=len(currencies):
            raise ValueError('Balances de vigilancia duplicados')
        if not 0<=int(self.received_at_ns)-int(self.requested_at_ns)<=FRESH_NS:
            raise ValueError('Recogida privada de posición antigua o regresada')
        return self


class PerpPositionReviewInput(ExactInput):
    schema_name: Literal['orbis.grvt.perp.position.input.v1']=Field(alias='schema')
    account_ref: str=Field(pattern=r'^[a-f0-9]{64}$')
    configurations: list[PositionConfiguration]=Field(min_length=2,max_length=2)
    account: PerpAccountPositionInput
    checked_at_ns: Nanoseconds


def position_account(raw: dict,*,requested_at_ns: int,received_at_ns: int) -> PerpAccountPositionInput:
    if (type(raw) is not dict or type(raw.get('positions')) is not list
            or type(raw.get('spot_balances')) is not list):
        raise ValueError('Cuenta o posiciones GRVT ausentes')
    positions=[]
    for row in raw['positions']:
        if type(row) is not dict:
            raise ValueError('Posición GRVT inválida')
        positions.append({'sub_account_id':row.get('sub_account_id'),'instrument':row.get('instrument'),
            'size':row.get('size'),'event_time_ns':row.get('event_time'),
            **{key:row.get(key) for key in ('margin_type','leverage','isolated_balance','isolated_im','isolated_mm')}})
    if any(type(row) is not dict for row in raw['spot_balances']):
        raise ValueError('Balance GRVT inválido')
    return PerpAccountPositionInput.model_validate({
        **{key:raw.get(key) for key in ('sub_account_id','settle_currency','margin_type','sub_account_mode','total_equity')},
        'event_time_ns':raw.get('event_time'),'requested_at_ns':str(requested_at_ns),
        'received_at_ns':str(received_at_ns),
        'balances':[{key:row.get(key) for key in ('currency','balance')} for row in raw['spot_balances']],
        'positions':positions})


@exact
def evaluate_position_review(raw: PerpPositionReviewInput,terms: PerpEntryTerms,*,expected_account_ref: str,
        entry_size: str,last_fill_ns: int,baseline_equity: str,settled_net: str,loss_cap: str,
        now_ns: int) -> dict:
    source=PerpPositionReviewInput.model_validate(raw.payload())
    terms=PerpEntryTerms.model_validate(terms.payload())
    account=source.account
    first,last=source.configurations
    checked=int(source.checked_at_ns)
    if (type(now_ns) is not int or not 0<now_ns<2**63 or type(last_fill_ns) is not int
            or not 0<last_fill_ns<=checked
            or source.account_ref!=expected_account_ref
            or account.sub_account_id!=terms.budget.sub_account_id
            or any(item.sub_account_id!=account.sub_account_id or item.instrument!=terms.instrument
                   for item in (first,last))
            or not int(first.requested_at_ns)<=int(first.received_at_ns)<=int(account.requested_at_ns)
                <=int(account.received_at_ns)<=int(last.requested_at_ns)<=int(last.received_at_ns)<=checked<=now_ns
            or now_ns-int(first.requested_at_ns)>FRESH_NS
            or not checked-FRESH_NS<=int(account.event_time_ns)<=int(account.received_at_ns)+2_000_000_000):
        raise ValueError('Vigilancia GRVT de otra cuenta, futura, antigua o sin cobertura')
    size=number(entry_size)
    if size<=0 or number(loss_cap)<=0:
        raise ValueError('Tamaño o límite de vigilancia inválido')
    reasons=[]
    if first.terms()!=last.terms():reasons.append('CONFIGURATION_CHANGED_DURING_READ')
    if last.margin_type!=terms.profile.margin_mode:reasons.append('MARGIN_MODE_MISMATCH')
    if leverage(last.leverage)!=leverage(terms.profile.leverage):reasons.append('LEVERAGE_MISMATCH')
    if not leverage(last.min_leverage)<=leverage(terms.profile.leverage)<=leverage(last.max_leverage):
        reasons.append('LEVERAGE_OUTSIDE_ACCOUNT_RANGE')
    if (account.settle_currency!='USDT' or account.margin_type!='SIMPLE_CROSS_MARGIN'
            or account.sub_account_mode!='SINGLE_ASSET_MODE'):
        reasons.append('ACCOUNT_MODE_CHANGED')
    if ('USDT' not in {row.currency for row in account.balances}
            or any(row.currency!='USDT' and number(row.balance)!=0 for row in account.balances)):
        reasons.append('OTHER_COLLATERAL')
    if number(account.total_equity)>number(terms.budget.collateral_limit):
        reasons.append('COLLATERAL_LIMIT')
    cycle_change=number(account.total_equity)-number(baseline_equity)
    total=number(settled_net)+cycle_change
    if total<=-number(loss_cap):reasons.append('LOSS_LIMIT')
    active=[row for row in account.positions if number(row.size)!=0]
    external=(len(active)>1 or any(row.instrument!=terms.instrument or row.sub_account_id!=account.sub_account_id for row in active))
    if external:
        reasons.append('EXTERNAL_POSITION')
    changed_size=False
    if not external and active:
        position=active[0]
        signed=size if terms.definition['direction']=='LONG' else -size
        changed_size=number(position.size)!=signed
        if position.margin_type!=terms.profile.margin_mode:reasons.append('POSITION_MARGIN_MODE_MISMATCH')
        if position.leverage is None or leverage(position.leverage)!=leverage(terms.profile.leverage):
            reasons.append('POSITION_LEVERAGE_MISMATCH')
        if (position.event_time_ns is None or not last_fill_ns<=int(position.event_time_ns)<=int(account.event_time_ns)+2_000_000_000):
            reasons.append('POSITION_TIME_UNVERIFIED')
        if terms.profile.margin_mode=='ISOLATED':
            values=(position.isolated_balance,position.isolated_im,position.isolated_mm)
            if any(value is None for value in values):
                reasons.append('ISOLATED_MARGIN_UNVERIFIED')
            elif any(number(value)<0 for value in values):
                reasons.append('ISOLATED_MARGIN_INVALID')
            elif number(position.isolated_balance)>number(terms.budget.collateral_limit):
                reasons.append('ISOLATED_COLLATERAL_LIMIT')
    elif not external:
        changed_size=True
    if changed_size:
        reasons.append('POSITION_SIZE_CHANGED')
    # A smaller/flat position can be a native fill between the order lookup and
    # this account snapshot. Recheck orders before calling it a risk breach.
    recheck=(reasons==['POSITION_SIZE_CHANGED'] and (not active or
        0<number(active[0].size)*(1 if terms.definition['direction']=='LONG' else -1)<size))
    action='REVIEW' if external else 'RECHECK' if recheck else 'CLOSE' if reasons else 'HOLD'
    return {'schema':'orbis.grvt.perp.position.result.v1','action':action,'reasons':reasons,
        'halt_new_entries':bool(reasons) and not recheck,'cycle_equity_change':format(cycle_change,'f'),
        'settled_plus_current_equity_change':format(total,'f'),
        'position_size':active[0].size if len(active)==1 and not external else None,
        'equity_change_scope':'DEDICATED_ACCOUNT_EXTERNAL_FLOWS_NOT_YET_RECONCILED',
        'checked_at_ns':source.checked_at_ns,'execution_authority':'NONE'}


def collect_position_review(client,terms: PerpEntryTerms,*,identity_salt: str,expected_account_ref: str,
                            require_permission,clock_ns=time.time_ns) -> PerpPositionReviewInput:
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient
    start=clock_ns();last=start
    def checked():
        nonlocal last
        require_permission()
        at=clock_ns()
        if (type(at) is not int or type(start) is not int or not 0<last<=at<2**63 or at-start>FRESH_NS
                or not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET'
                or client.sub_account_id!=terms.budget.sub_account_id
                or client.identity_ref(identity_salt)!=expected_account_ref):
            raise ValueError('Vigilancia revocada, antigua o de otra identidad')
        last=at
        return at
    try:
        checked()
        first=client.position_configuration(terms.budget.sub_account_id,terms.instrument)
        checked();requested=checked()
        raw=client.read_account('account_summary',terms.budget.sub_account_id)['result']
        received=checked()
        account=position_account(raw,requested_at_ns=requested,received_at_ns=received)
        checked()
        final=client.position_configuration(terms.budget.sub_account_id,terms.instrument)
        at=checked()
        return PerpPositionReviewInput(schema_name='orbis.grvt.perp.position.input.v1',
            account_ref=expected_account_ref,configurations=[first,final],account=account,checked_at_ns=str(at))
    except Exception:
        raise GrvtMainnetError('Vigilancia de posición perpetua no verificable') from None
