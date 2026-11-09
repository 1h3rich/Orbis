"""Versioned supervision replay. Saved safety decisions never authorize orders."""
from __future__ import annotations

from dataclasses import dataclass,replace
from decimal import Decimal
import hashlib
import json

from app.lab.perp_book_types import exact
from app.lab.store import encoded
from app.live_execution.grvt_mainnet_bot_exit import entry_from_fills
from app.live_execution.grvt_mainnet_bracket import unsigned_mainnet_bracket
from app.live_execution.grvt_mainnet_journal import _draft,_instrument,encode
from app.live_execution.grvt_mainnet_observation import _check_states
from app.live_execution.grvt_mainnet_preflight import number
from app.live_execution.grvt_perp_exit import (
    PerpExitCursor,observe_perp_exit,perp_exit_checkpoint,start_perp_exit,
)
from app.live_execution.grvt_perp_plan import PerpEntryTerms
from app.live_execution.grvt_perp_position import PerpPositionReviewInput,evaluate_position_review
from app.live_execution.grvt_perp_signals import MAX_CONSUMER_DELAY_NS,MAX_QUOTE_GAP_NS,PerpSignalState
from app.portfolio.models import GrvtMainnetBracket,GrvtMainnetEntryFills,GrvtMainnetObservation


SUPERVISION_ENGINE='grvt-perp-supervision-1.0'
SUPERVISED_SCHEMA='orbis.grvt.mainnet.bot.grant.v5'
SOURCE_FAULTS=frozenset(('SOURCE_ERROR','ARCHIVE_ERROR','ARCHIVE_DELAY','START_ERROR'))


@dataclass(frozen=True)
class PerpSupervision:
    parent: str|None=None
    entry_json: str|None=None
    entry_digest: str|None=None
    account_ref: str|None=None
    baseline_equity: str|None=None
    cursor: PerpExitCursor|None=None
    halt_reason: str|None=None
    risk_action: str|None=None
    position_result_json: str|None=None


def supervision_checkpoint(state: PerpSupervision) -> dict:
    position=json.loads(state.position_result_json) if state.position_result_json is not None else None
    if state.parent is None:
        action='REVIEW' if state.halt_reason is not None else 'IDLE'
    elif state.risk_action=='REVIEW':action='REVIEW'
    elif state.risk_action=='CLOSE' or state.cursor is not None and state.cursor.action=='CLOSE':action='CLOSE'
    elif position is not None and position['action']=='RECHECK':action='RECHECK'
    else:action='HOLD'
    return {'schema':'orbis.grvt.perp.supervision.checkpoint.v1','engine':SUPERVISION_ENGINE,
        'parent_client_order_id':state.parent,'entry_digest':state.entry_digest,
        'entry_state':'CONFIRMED_OWN_FILLS' if state.entry_json is not None else 'AWAITING_FILLS' if state.parent else 'NONE',
        'action':action,'halt_new_entries':state.halt_reason is not None,'halt_reason':state.halt_reason,
        'exit':perp_exit_checkpoint(state.cursor) if state.cursor is not None else None,
        'position':position,'execution_authority':'NONE'}


def visible_select(checkpoint: dict,parent: str) -> dict:
    # Metadata only for an already verified hot prefix, never an executable
    # cursor reconstructed from a persisted checkpoint.
    return {**checkpoint,'parent_client_order_id':parent,'entry_digest':None,'entry_state':'AWAITING_FILLS',
        'action':'HOLD','exit':None,'position':None}


def visible_settle(checkpoint: dict) -> dict:
    return {**checkpoint,'parent_client_order_id':None,'entry_digest':None,'entry_state':'NONE',
        'action':'REVIEW' if checkpoint['halt_new_entries'] else 'IDLE','exit':None,'position':None}


def select_supervision(state: PerpSupervision,parent: str) -> PerpSupervision:
    if state.parent is not None or state.halt_reason is not None:
        raise ValueError('Supervisión ocupada o detenida')
    return replace(state,parent=parent)


def source_fault_supervision(state: PerpSupervision,reason: str) -> PerpSupervision:
    if reason not in SOURCE_FAULTS:
        raise ValueError('Fallo de fuente desconocido')
    return replace(state,halt_reason=state.halt_reason or reason,
        risk_action='CLOSE' if state.parent is not None and state.risk_action!='REVIEW' else state.risk_action)


@exact
def entry_material(db,parent: str,digest: str,selected: dict) -> tuple[dict,dict]:
    row=db.get(GrvtMainnetEntryFills,parent)
    claimrow=db.get(GrvtMainnetBracket,parent)
    if row is None or claimrow is None or row.payload_digest!=digest:
        raise ValueError('Fills confirmados de supervisión ausentes')
    entry=json.loads(row.payload_json);claim=json.loads(claimrow.payload_json)
    if (set(entry)!={'schema','network','parent_client_order_id','claim_digest','observation_sequence','observation_digest',
            'cutoff_ns','fills','size','notional','last_fill_ns'}
            or entry['schema']!='orbis.grvt.mainnet.entry.fills.v1' or entry['network']!='MAINNET'
            or entry['parent_client_order_id']!=parent or encode(entry)!=(row.payload_json,row.payload_digest)
            or encode(claim)!=(claimrow.payload_json,claimrow.payload_digest)
            or claim['schema']!='orbis.grvt.mainnet.bracket.claim.v3'
            or claim['source']!='PERP_BOT_NATIVE_PROTECTED_ENTRY'
            or claim['preflight']['plan']!=selected['plan'] or entry['claim_digest']!=claimrow.payload_digest
            or type(entry['observation_sequence']) is not int or entry['observation_sequence']<1
            or type(entry['cutoff_ns']) is not int or not 0<entry['cutoff_ns']<2**63):
        raise ValueError('Fills o claim ajenos al plan supervisado')
    obsrow=db.get(GrvtMainnetObservation,(parent,entry['observation_sequence']))
    if obsrow is None or obsrow.payload_digest!=entry['observation_digest']:
        raise ValueError('Observación original de entrada ausente')
    observed=json.loads(obsrow.payload_json)
    if (encode(observed)!=(obsrow.payload_json,obsrow.payload_digest)
            or observed['parent_client_order_id']!=parent or observed['claim_digest']!=claimrow.payload_digest
            or not 0<=entry['cutoff_ns']-observed['state']['observed_at_ns']<=120_000_000_000):
        raise ValueError('Observación original de entrada cambiada')
    draft=_draft(claim['draft']);instrument=_instrument(claim['instrument'])
    templates=unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=parent,
        nonces=tuple(claim['nonces']),issued_at_ns=claim['issued_at_ns'])
    _check_states(observed['state']['orders'],templates,at=observed['state']['observed_at_ns'],
        issued=claim['issued_at_ns'],previous=None,ack=None)
    size,notional,last=entry_from_fills(claim,observed,entry['fills'],cutoff_ns=entry['cutoff_ns'])
    if (entry['size'],entry['notional'],entry['last_fill_ns'])!=(format(size,'f'),format(notional,'f'),last):
        raise ValueError('Cantidades de supervisión modificadas')
    return entry,claim


def supervision_dependencies(db,parents: tuple[str,...]) -> tuple:
    result=[]
    for parent in parents:
        entry=db.get(GrvtMainnetEntryFills,parent)
        claim=db.get(GrvtMainnetBracket,parent)
        if entry is None or claim is None:
            raise ValueError('Dependencia de supervisión desaparecida')
        parsed=json.loads(entry.payload_json)
        observation=db.get(GrvtMainnetObservation,(parent,parsed['observation_sequence']))
        if observation is None:
            raise ValueError('Observación de supervisión desaparecida')
        for kind,row in (('entry',entry),('claim',claim),('observation',observation)):
            result.append((kind,parent,getattr(row,'sequence',None),row.payload_digest,
                           hashlib.sha256(row.payload_json.encode()).hexdigest()))
    return tuple(result)


@exact
def confirm_supervision_entry(state: PerpSupervision,feed: PerpSignalState,entry: dict,claim: dict,
                              terms: PerpEntryTerms,*,now_ns: int) -> PerpSupervision:
    if (state.parent!=entry['parent_client_order_id'] or state.entry_json is not None
            or not entry['last_fill_ns']<=entry['cutoff_ns']<=now_ns):
        raise ValueError('Confirmación de entrada supervisada fuera de orden')
    cursor=start_perp_exit(feed,size=number(entry['size']),entry_notional=number(entry['notional']),
        last_fill_ns=entry['last_fill_ns'],confirmed_at_ns=now_ns)
    ceiling=number(terms.budget.fee_rate_ceiling)
    exceeded=any(number(row['fee'])>number(row['size'])*number(row['price'])*ceiling for row in entry['fills']['rows'])
    updated=replace(state,entry_json=encoded(entry),entry_digest=encode(entry)[1],account_ref=claim['account_ref'],
        baseline_equity=claim['preflight']['states'][-1]['total_equity'],cursor=cursor,
        halt_reason=state.halt_reason or ('FEE_CEILING_EXCEEDED' if exceeded else None),
        risk_action='CLOSE' if exceeded else state.risk_action)
    return market_supervision(updated,feed,now_ns=now_ns)


def market_supervision(state: PerpSupervision,feed: PerpSignalState,*,now_ns: int) -> PerpSupervision:
    if feed.status!='RUNNING' or now_ns-feed.last_observed_ns>MAX_QUOTE_GAP_NS:
        state=replace(state,halt_reason=state.halt_reason or 'DATA_GAP',
                      risk_action='CLOSE' if state.parent is not None else state.risk_action)
    if state.parent is not None and state.cursor is None and now_ns-feed.last_observed_ns>MAX_CONSUMER_DELAY_NS:
        state=replace(state,halt_reason=state.halt_reason or 'DATA_STALE',risk_action='CLOSE')
    if state.cursor is None:
        return state
    cursor=observe_perp_exit(state.cursor,feed,now_ns=now_ns)
    return replace(state,cursor=cursor,halt_reason=state.halt_reason or
        (cursor.reason if cursor.reason in ('DATA_GAP','DATA_STALE') else None))


@exact
def position_supervision(state: PerpSupervision,raw: PerpPositionReviewInput,terms: PerpEntryTerms,
                         policy,*,settled_net: str,now_ns: int) -> tuple[PerpSupervision,dict]:
    if state.entry_json is None or state.account_ref is None or state.baseline_equity is None:
        raise ValueError('Vigilancia sin entrada propia confirmada')
    entry=json.loads(state.entry_json)
    result=evaluate_position_review(raw,terms,expected_account_ref=state.account_ref,
        entry_size=entry['size'],last_fill_ns=entry['last_fill_ns'],baseline_equity=state.baseline_equity,
        settled_net=settled_net,loss_cap=format(policy.loss_cap,'f'),now_ns=now_ns)
    halt=state.halt_reason
    action=state.risk_action
    if result['halt_new_entries']:
        halt=halt or result['reasons'][0]
        if result['action']=='REVIEW':action='REVIEW'
        elif action!='REVIEW':action='CLOSE'
    return replace(state,position_result_json=encoded(result),halt_reason=halt,risk_action=action),result


def settle_supervision(state: PerpSupervision,parent: str) -> PerpSupervision:
    if state.parent!=parent:
        raise ValueError('Recibo de otra posición supervisada')
    return PerpSupervision(halt_reason=state.halt_reason)


@exact
def settled_net(selections: list[dict]) -> str:
    return format(sum((number(item['settlement']['net_realized_pnl_usdt']) for item in selections
                       if item['settlement'] is not None),Decimal(0)),'f')
