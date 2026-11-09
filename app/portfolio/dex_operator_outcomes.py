"""Read-only operator facts: original receipts and independently sealed outcomes.

Economic outcomes are distinct from EVM status: a successful self-cancellation
has no trade. Historical readers cannot adopt facts accepted after their cutoff.
"""
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal,Mapping,Any
import json

from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio import dex_operator_journal as original
from app.portfolio.wallet_funding import _time,_uint


def _freeze(value):
    if isinstance(value,dict):return MappingProxyType({key:_freeze(item) for key,item in value.items()})
    if isinstance(value,list):return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True,slots=True)
class OperatorOutcome:
    id:str
    account_id:str
    grant_id:str
    action:str
    definition:Mapping[str,Any]
    outcome:Literal['PENDING','ABANDONED_UNSENT','EXECUTED','CANCELLED','REVERTED','EXTERNAL_REVIEWED']
    receipt_kind:Literal['ORIGINAL','CALL_RESOLUTION','EXTERNAL_REVIEW']|None
    accepted_at:datetime|None
    receipt:Mapping[str,Any]|None
    effect:Mapping[str,Any]|None
    gas_fee_wei:int
    resolution_id:str|None
    resolution_digest:str|None
    external_witness_json:str|None=None

    @property
    def settled(self)->bool:return self.outcome in {'EXECUTED','CANCELLED','REVERTED','EXTERNAL_REVIEWED'}

    @property
    def filled(self)->bool:return self.outcome=='EXECUTED' and self.action in {'BUY','SELL'}

    def witness(self)->dict|None:
        if self.external_witness_json is not None:
            return {'intent_id':self.id,'external_review':json.loads(self.external_witness_json)}
        if self.resolution_id is None:return None
        return {'intent_id':self.id,'resolution_id':self.resolution_id,'payload_digest':self.resolution_digest}


def operator_outcome(db,identifier:str,*,as_of:datetime|None=None,include_external:bool=False)->OperatorOutcome:
    row,terms,state=original.intent_record(db,identifier)
    cutoff=_time(as_of) if as_of is not None else None
    if cutoff is not None and row.created_at>cutoff:raise DexExecutorError('La intención aún no existía en esta observación')
    outcome='PENDING';kind=accepted=receipt=effect=resolution_id=resolution_digest=None
    external_json=None
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
        from app.portfolio import dex_call_resolution as resolutions
        record=resolutions.resolution_record(db,resolutions.identifier('OPERATOR',row.id))
        if record is not None:
            resolved,payload=record
            # Always authenticate a present record, even when it is too recent
            # for this historical projection. Corruption isn't a pending fact.
            if cutoff is None or resolved.created_at<=cutoff:
                proof=payload['proof'];outcome=proof['outcome'];kind='CALL_RESOLUTION'
                accepted=resolved.created_at;receipt=proof['receipt'];effect=proof['effect']
                resolution_id=resolved.id;resolution_digest=resolved.payload_digest
    if include_external and kind is None:
        from app.portfolio.dex_external_outcomes import external_outcome
        from app.portfolio.wallet_funding import _encoded
        external=external_outcome(db,'OPERATOR',row.id,as_of=cutoff)
        if external is not None:
            outcome='EXTERNAL_REVIEWED';kind='EXTERNAL_REVIEW';accepted=external.accepted_at
            receipt=json.loads(external.receipt_json);external_json=_encoded(external.witness())[0]
    if kind is None and (cutoff is None or row.updated_at<=cutoff):
        if row.status in {'FINALIZED','REVERTED'}:
            outcome='EXECUTED' if row.status=='FINALIZED' else 'REVERTED';kind='ORIGINAL';accepted=row.updated_at;receipt=state['receipt']
            if outcome=='EXECUTED':
                from app.portfolio.dex_operator_receipt import FINANCIAL
                effect={key:receipt[key] for key in FINANCIAL}
        elif row.status=='ABANDONED_UNSENT':outcome='ABANDONED_UNSENT';accepted=row.updated_at
    return OperatorOutcome(row.id,row.account_id,row.grant_id,terms['action'],_freeze(terms),outcome,kind,accepted,
        _freeze(receipt) if receipt is not None else None,_freeze(effect) if effect is not None else None,
        int(receipt['gas_fee_wei']) if receipt is not None else 0,resolution_id,resolution_digest,external_json)


def outcome_witnesses(db,identifiers,*,as_of:datetime|None=None,include_external:bool=False)->list[dict]:
    result=[]
    for identifier in identifiers:
        fact=operator_outcome(db,identifier,as_of=as_of,include_external=include_external)
        if not fact.settled:raise DexExecutorError('La ejecución aún no estaba resuelta en esta observación')
        witness=fact.witness()
        if witness is not None:result.append(witness)
    return sorted(result,key=lambda item:item['intent_id'])


@dataclass(frozen=True,slots=True)
class PositionProjection:
    counters:Mapping[str,int]
    entry_candle_t:int|None
    gas_spent_wei:int


def fold_operator_position(baseline:Mapping,executions:tuple[tuple[OperatorOutcome,int|None],...],*,entry_candle_t:int|None=None)->PositionProjection:
    """Fold verified economic outcomes, preserving physical contract counters."""
    if type(executions) is not tuple or entry_candle_t is not None and type(entry_candle_t) is not int:raise ValueError('position scope')
    counters={}
    for key,value in baseline.items():
        if type(value) is int:
            if not 0<=value<2**256:raise ValueError('position counter')
            counters[key]=value
        elif type(value) is str:counters[key]=_uint(value,key,positive=False)
        else:raise ValueError('position counter')
    entry=entry_candle_t
    identities=set();last_nonce=-1;gas=0
    for fact,candle_t in executions:
        if type(fact) is not OperatorOutcome or not fact.settled or fact.id in identities or fact.action not in {'BUY','SELL'}:raise ValueError('position outcome')
        recipe=fact.definition;nonce=int(recipe['nonce'])
        if nonce<=last_nonce or {key:int(value) for key,value in recipe['anchor']['counters'].items()}!=counters:raise ValueError('position lineage')
        identities.add(fact.id);last_nonce=nonce;gas+=fact.gas_fee_wei
        if fact.outcome!='EXECUTED':continue
        effect=fact.effect
        if effect is None or fact.receipt is None:raise ValueError('missing executed effect')
        counters['next_nonce']+=1
        counters['owned_wbnb'],counters['cost_usdt']=int(effect['owned_wbnb']),int(effect['cost_usdt'])
        if fact.action=='BUY':
            if type(candle_t) is not int:raise ValueError('entry candle')
            counters['buy_spent']+=int(recipe['trade']['amount']);counters['entries']+=1
            counters['last_entry_at']=int(datetime.fromisoformat(fact.receipt['block_at']).timestamp())
            counters['acquired_wbnb']+=int(effect['amount_out_units']);entry=candle_t
        else:
            counters['realized_loss']+=max(0,-int(effect['realized_pnl_usdt']))
            if not counters['owned_wbnb']:entry=None
    return PositionProjection(_freeze(counters),entry,gas)
