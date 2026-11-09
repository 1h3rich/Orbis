"""Verified permission epochs, without resetting the existing policy or gas.

Chain traversal is iterative. Each predecessor is semantically verified before
its successor; a stored reference alone never supplies position or authority.
"""
from datetime import datetime
from decimal import Decimal,localcontext
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_plan import DexBotCursor
from app.live_execution.dex_permission import LONG_ENGINE_V3,LONG_ENGINE_V4
from app.live_execution import dex_permission as permission_versions
LONG_ENGINE_V5=getattr(permission_versions,'LONG_ENGINE_V5','orbis.dex.bot.long.v5')
from app.live_execution.dex_versions import LONG_POLICY_OUTCOME,EXTERNAL_LONG_ENGINES
from app.portfolio.models import RealDexBotRun,RealDexBotBinding
from app.portfolio.wallet_funding import _decoded,_time


def ordered_predecessors(db:Session,identifier:str)->list[str]:
    """Return a flat root-to-leaf list and reject cycles or forked epochs."""
    def root_id(binding_id):
        row=db.get(RealDexBotBinding,binding_id)
        if row is None:raise DexExecutorError('Falta el vínculo de un permiso anterior')
        terms=_decoded(row.payload_json,row.payload_digest)
        return terms.get('root_binding_id',row.id)
    items=[];seen=set();binding=None;root=None
    while True:
        identifier=str(UUID(identifier))
        if identifier in seen:raise DexExecutorError('El linaje del bot contiene un ciclo')
        seen.add(identifier);row=db.get(RealDexBotRun,identifier)
        if row is None:raise DexExecutorError('Falta un permiso anterior del bot')
        if binding is None:binding=row.binding_id;root=root_id(binding)
        if root_id(row.binding_id)!=root:raise DexExecutorError('El linaje pertenece a otro bot')
        terms=_decoded(row.payload_json,row.payload_digest);items.append(row.id)
        if terms.get('schema') not in (LONG_ENGINE_V3,LONG_ENGINE_V4,LONG_ENGINE_V5,LONG_POLICY_OUTCOME,*EXTERNAL_LONG_ENGINES):break
        identifier=terms['resume']['previous_run_id']
    successors={}
    family=[row.id for row in db.scalars(select(RealDexBotBinding)) if root_id(row.id)==root]
    for row in db.scalars(select(RealDexBotRun).where(RealDexBotRun.binding_id.in_(family))):
        terms=_decoded(row.payload_json,row.payload_digest)
        if terms.get('schema') not in (LONG_ENGINE_V3,LONG_ENGINE_V4,LONG_ENGINE_V5,LONG_POLICY_OUTCOME,*EXTERNAL_LONG_ENGINES):continue
        parent=terms['resume']['previous_run_id']
        if parent in successors:raise DexExecutorError('Dos permisos retoman la misma sesión del bot')
        successors[parent]=row.id
    return list(reversed(items))


def resume_material(db:Session,parent:tuple,*,anchor:dict,seed:list[dict],at:datetime,child_id:str,include_resolutions:bool=False,include_external:bool=False)->tuple[dict,DexBotCursor]:
    """Derive the only admissible baseline/cursor from finalized own receipts."""
    from app.portfolio.dex_bot_journal import _position,_settled,_cursor_json
    row,terms,state,archive=parent;at=_time(at)
    if row.status!='STOPPED' or at<=row.updated_at:
        raise DexExecutorError('Detén y revisa el permiso anterior antes de rearmar')
    later=db.scalar(select(RealDexBotRun.id).where(RealDexBotRun.binding_id==row.binding_id,
        RealDexBotRun.id.not_in((row.id,child_id)),RealDexBotRun.created_at>=row.created_at,RealDexBotRun.created_at<=at).limit(1))
    if later is not None:raise DexExecutorError('Rearma el último permiso del vínculo, no uno anterior')
    witnesses=None
    if include_resolutions:
        settled=_settled(db,row.grant_id,run_id=row.id,include_resolutions=True,as_of=at,include_external=include_external)
        from app.portfolio.dex_operator_outcomes import outcome_witnesses
        witnesses=outcome_witnesses(db,settled,as_of=at,include_external=include_external)
        position,entry=_position(db,row,terms,settled,at,resolved_calls=witnesses,include_external=include_external)
    else:
        # A previously loaded v82 hub has the original helper signatures.
        settled=_settled(db,row.grant_id,run_id=row.id)
        position,entry=_position(db,row,terms,settled,at)
    if position!={key:int(value) for key,value in anchor['counters'].items()}:
        raise DexExecutorError('La posición o los contadores cambiaron desde los recibos del bot')
    previous=[bar.json() for bar in archive['bars']]
    if len(seed)<len(previous) or seed[:len(previous)]!=previous:
        raise DexExecutorError('El histórico de recuperación no conserva las velas anteriores')
    cursor=archive['cursor'];high=closing=None
    if position['owned_wbnb']:
        if entry is None:raise DexExecutorError('Falta la procedencia de la posición abierta')
        with localcontext() as context:
            context.prec=300
            average=Decimal(position['cost_usdt'])/Decimal(position['owned_wbnb'])
            high=str(max(average,Decimal(cursor.trailing_high) if cursor.trailing_high is not None else average))
        closing=cursor.closing_reason
    initial=DexBotCursor(seed[-1]['t'],at,entry,high,closing)
    result={'previous_run_id':row.id,'previous_payload_digest':row.payload_digest,'previous_state_digest':row.state_digest,
        'settled_ids':settled,'initial_cursor':_cursor_json(initial),
        'observation_gap':{'from':cursor.last_checked_at.isoformat(),'to':at.isoformat()}}
    if witnesses is not None:result['resolved_calls']=witnesses
    return result,initial
