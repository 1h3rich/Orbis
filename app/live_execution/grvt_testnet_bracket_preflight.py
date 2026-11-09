"""Revisión archivada del grupo completo; no sustituye el replay causal."""

from dataclasses import asdict
from decimal import Decimal
from sqlalchemy.orm import Session

from app.derivatives.paper_math import MarginRules
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError,compile_testnet_bracket,BRACKET_FIELDS
from app.live_execution.grvt_testnet_oco import _encode,_number
from app.live_execution.grvt_testnet_oco_preflight import _safe,_signed,_MONEY
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskState
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument,GrvtTestnetLimitDraft
from app.live_execution.grvt_testnet_continuity import validate_testnet_history_continuity
from app.live_execution.grvt_testnet_reconciliation import require_reconciled_continuity


def make_bracket_preflight(plan: dict,state: GrvtTestnetRiskState,rules: MarginRules,quote: GrvtTestnetQuote,
                           history: dict | None,*,checked_at_ns: int,signal_recorded_at_ns: int) -> dict:
    return {'schema':'orbis.grvt.testnet.bracket.preflight.v1',
        'parent_client_order_id':plan['parent_client_order_id'],'plan_digest':_encode({key:plan[key] for key in BRACKET_FIELDS})[1],
        'checked_at_ns':checked_at_ns,'signal_recorded_at_ns':signal_recorded_at_ns,
        'state':_safe(asdict(state)),'rules':rules.json(),'quote':_safe(asdict(quote)),
        'history_continuity':history}


def verify_bracket_preflight(db: Session,plan: dict,proof: object,*,now_ns: int,historical: bool=False) -> None:
    try:
        fields={'schema','parent_client_order_id','plan_digest','checked_at_ns','signal_recorded_at_ns',
            'state','rules','quote','history_continuity'}
        if (not isinstance(proof,dict) or set(proof)!=fields
                or proof['schema']!='orbis.grvt.testnet.bracket.preflight.v1'
                or proof['parent_client_order_id']!=plan['parent_client_order_id']
                or proof['plan_digest']!=_encode(plan)[1]
                or type(now_ns) is not int or type(proof['checked_at_ns']) is not int
                or type(proof['signal_recorded_at_ns']) is not int
                or not 0<proof['signal_recorded_at_ns']<=plan['created_at_ns']<=proof['checked_at_ns']<=now_ns
                or now_ns-proof['checked_at_ns']>5_000_000_000
                or now_ns-proof['signal_recorded_at_ns']>20_000_000_000
                or not now_ns<plan['legs'][0]['expiration_ns']<=proof['signal_recorded_at_ns']+20_000_000_000):
            raise ValueError('invalid bracket preflight')
        raw=proof['state']
        if (not isinstance(raw,dict) or set(raw)!=set(GrvtTestnetRiskState.__dataclass_fields__)
                or any(type(raw[key]) is not int for key in ('event_time_ns','observed_at_ns','open_order_count'))
                or raw['positions']!=[]):
            raise ValueError('nonflat or untyped account')
        state=GrvtTestnetRiskState(**{**raw,**{key:_signed(raw[key]) for key in _MONEY},'positions':()})
        if state.initial_margin<0 or state.maintenance_margin<0: raise ValueError('negative margin')
        entry_limits=GrvtTestnetLimits(plan['sub_account_id'],plan['instrument'],_number(plan['entry_cap']))
        exit_limits=GrvtTestnetLimits(plan['sub_account_id'],plan['instrument'],_number(plan['exit_cap']))
        raw_rules=proof['rules']
        if not isinstance(raw_rules,dict) or type(raw_rules.get('fetched_at_ns')) is not int:
            raise ValueError('untyped rules time')
        rules=MarginRules.from_payload(raw_rules,plan['instrument'],now_ns)
        q=proof['quote']
        if (not isinstance(q,dict) or set(q)!=set(GrvtTestnetQuote.__dataclass_fields__)
                or any(type(q[key]) is not int for key in ('event_time_ns','observed_at_ns'))):
            raise ValueError('untyped quote')
        quote=GrvtTestnetQuote(**{**q,**{key:_number(q[key]) for key in ('bid','ask','bid_size','ask_size','last_price')}})
        terms=plan['instrument_terms']
        instrument=GrvtTestnetInstrument(plan['instrument'],int(terms['asset_id']),terms['base_decimals'],
            _number(terms['tick_size']),_number(terms['min_size']),_number(terms['min_notional']))
        parent,stop,target=plan['legs']
        draft=GrvtTestnetLimitDraft(plan['sub_account_id'],plan['instrument'],parent['side'],
            _number(parent['size']),_number(parent['limit_price']),False,'0x'+'0'*40,
            parent['client_order_id'],parent['nonce'],parent['expiration_ns'])
        compiled=compile_testnet_bracket(draft,state,rules,instrument,entry_limits,exit_limits,quote,
            stop_loss=_number(stop['trigger_price']),take_profit=_number(target['trigger_price']),
            **plan['price_policy'],exit_nonces=(stop['nonce'],target['nonce']),
            exit_expiration_ns=stop['expiration_ns'],now_ns=now_ns)
        if any(child.limit_price!=Decimal(leg['limit_price']) or child.size!=Decimal(leg['size'])
               for child,leg in zip((compiled.stop_loss,compiled.take_profit),(stop,target))):
            raise ValueError('child terms changed')
        require_reconciled_continuity(db,plan['sub_account_id'],raw,now_ns=now_ns,historical=historical)
        validate_testnet_history_continuity(db,plan['sub_account_id'],proof['history_continuity'],now_ns=now_ns,
            account_event_time_ns=state.event_time_ns,client_order_id=plan['parent_client_order_id'],historical=historical)
    except Exception:
        raise GrvtTestnetBracketError('Preflight de entrada OTOCO no verificable') from None
