"""Owner-reviewed policy renewal from verified lifetime consumption.

This module does not sign, send, fund or start workers. Policy counters may
reset on chain; the original authorization and confirmed costs do not.
"""
from dataclasses import replace
from datetime import datetime
from decimal import Decimal,localcontext
from uuid import UUID,uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_plan import DexBotCursor
from app.live_execution.dex_dca_plan import ENGINE as DCA_ENGINE
from app.live_execution.dex_policy import PolicyLimits
from app.live_execution.dex_trade import STOP_GAS_LIMIT
from app.portfolio import dex_bot_bindings as bindings,dex_bot_journal as bots,dex_operator_journal as operator
from app.portfolio.dex_controls import _anchor,_context
from app.portfolio.dex_deployments import _identity as wallet_identity
from app.lab.store import content_hash
from app.portfolio.models import RealDexBotRun,RealDexBotDecision,RealDexOperatorIntent,RealDexBotBinding,RealDexControlIntent
from app.portfolio.wallet_funding import _time,_encoded,_decoded,exact_units
from app.portfolio.wallet_proof import canonical_wallet_address
from app.live_execution.dex_versions import (SUCCESSOR_BINDINGS,ROLLOVER_CONTROLS,
    RECOVERED_CONTROL,RECOVERED_BINDING,RECOVERED_LONG_ROLLOVER,RECOVERED_DCA_ROLLOVER,
    LONG_POLICY_OUTCOME,DCA_POLICY_OUTCOME,EXTERNAL_CONTROL,EXTERNAL_BINDING,
    EXTERNAL_LONG_ROLLOVER,EXTERNAL_DCA_ROLLOVER,EXTERNAL_LONG_ENGINES,EXTERNAL_DCA_ENGINES,DCA_EXTERNAL_POLICY)

DCA_ENGINES=(DCA_ENGINE,'orbis.dex.bot.dca.v2',DCA_POLICY_OUTCOME,*EXTERNAL_DCA_ENGINES)

CONTROL_SCHEMA='orbis.dex.control.v3'
ROLLOVER_SCHEMA='orbis.dex.bot.policy-rollover.v1'


def remaining_limits(root:PolicyLimits,consumed:dict,*,owned_wbnb:int,expires_at:int,at:datetime)->PolicyLimits:
    """Exact remanents, including a loss ceiling already exceeded by a sale."""
    keys={'buy_spent','entries','realized_loss','acquired_wbnb','gas_spent','last_entry_at'}
    if set(consumed)!=keys or any(type(value) is not int or value<0 for value in consumed.values()):
        raise DexExecutorError('Consumos acumulados no válidos')
    if consumed['buy_spent']>root.buy_budget or consumed['entries']>root.max_entries or consumed['acquired_wbnb']>root.max_wbnb_acquired:
        raise DexExecutorError('Los consumos superan la autorización original')
    budget=root.buy_budget-consumed['buy_spent'];entries=root.max_entries-consumed['entries']
    loss=max(0,root.max_realized_loss-consumed['realized_loss']);quota=root.max_wbnb_acquired-consumed['acquired_wbnb']
    buy=root.allow_buy and all(value>0 for value in (budget,entries,loss,quota))
    if not buy and not (root.allow_sell and owned_wbnb>0):
        raise DexExecutorError('Los límites de compra están agotados y no queda posición que cerrar')
    limits=replace(root,expires_at=expires_at,allow_buy=buy,
        cooldown_seconds=root.cooldown_seconds if buy else 0,max_entries=entries if buy else 0,
        max_buy_per_order=min(root.max_buy_per_order,budget) if buy else 0,buy_budget=budget if buy else 0,
        max_exposure=root.max_exposure if buy else 0,max_realized_loss=loss if buy else 0,
        max_wbnb_acquired=owned_wbnb+quota)
    limits.validate_at(int(at.timestamp()),owned_wbnb)
    return limits


def _settled_at(db:Session,grant_id:str,at:datetime,*,include_resolutions:bool=False,include_external:bool=False)->list[tuple]:
    values=[]
    for item in db.scalars(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==grant_id,RealDexOperatorIntent.created_at<=at)):
        row,recipe,state=operator.intent_record(db,item.id)
        if row.updated_at>at:raise DexExecutorError('Había una operación pendiente al revisar la renovación')
        if row.status=='ABANDONED_UNSENT':continue
        if include_resolutions:
            from app.portfolio.dex_operator_outcomes import operator_outcome
            if not operator_outcome(db,row.id,as_of=at,include_external=include_external).settled:
                raise DexExecutorError('Resuelve la operación pendiente antes de renovar')
        elif row.status not in {'FINALIZED','REVERTED'}:raise DexExecutorError('Resuelve la operación pendiente antes de renovar')
        decision=None if recipe['action']=='STOP' else db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.payload_digest==recipe['decision_ref']))
        if recipe['action']!='STOP' and decision is None:raise DexExecutorError('Falta la procedencia de una ejecución del bot')
        values.append((int(recipe['nonce']),row,recipe,state,decision))
    return sorted(values,key=lambda item:(item[0],item[1].id))


def _policy_stops(db,parent,grant,position,anchor,settled,at,*,include_resolutions:bool=False,include_external:bool=False):
    """Each changed policy revision needs its exact finalized STOP receipt."""
    candidates=[]
    for _,row,terms,state,_ in settled:
        if terms['action']!='STOP':continue
        if include_resolutions:
            from app.portfolio.dex_operator_outcomes import operator_outcome
            fact=operator_outcome(db,row.id,as_of=at,include_external=include_external)
            if fact.outcome=='EXECUTED':
                candidates.append(('OPERATOR',row,terms,state,{**fact.receipt,**fact.effect}))
        elif row.status=='FINALIZED':candidates.append(('OPERATOR',row,terms,state,state['receipt']))
    from app.portfolio.dex_controls import control_record
    for row in db.scalars(select(RealDexControlIntent).where(RealDexControlIntent.deployment_id==parent.deployment_id,
            RealDexControlIntent.status=='FINALIZED',RealDexControlIntent.created_at<=at,RealDexControlIntent.updated_at<=at)):
        # Do not recurse through the CONFIGURE whose provenance is being read.
        if _decoded(row.payload_json,row.payload_digest).get('action')=='STOP':
            item,terms,state=control_record(db,row.id);candidates.append(('OWNER',item,terms,state,state['receipt']))
    observed={key:int(value) for key,value in anchor['counters'].items()}
    current=position['policy_id'];target=observed['policy_id']
    if (any(observed[key]!=value for key,value in position.items() if key!='policy_id')
            or target<current or target-current>len(candidates)):
        raise DexExecutorError('La política o posición cambió fuera del historial del bot')
    if target==current:
        if (anchor['operator_ref']!=grant['anchor']['operator_ref']
                or any(int(receipt['new_policy_id'])>current for _,_,_,_,receipt in candidates)):
            raise DexExecutorError('La política o parada observada no conserva el historial del bot')
        return []
    if anchor['operator_ref'] is not None:raise DexExecutorError('La nueva revisión no corresponde a una parada confirmada')
    result=[];operator_ref=grant['anchor']['operator_ref']
    for _ in range(target-current):
        matches=[item for item in candidates if int(item[2]['anchor']['counters']['policy_id'])==current
            and int(item[4]['new_policy_id'])==current+1]
        if len(matches)!=1:raise DexExecutorError('Falta el recibo original de cada parada de política')
        kind,row,terms,state,receipt=matches[0]
        if (terms['anchor']['operator_ref']!=operator_ref
                or receipt['owned_wbnb']!=str(position['owned_wbnb']) or receipt['cost_usdt']!=str(position['cost_usdt'])
                or int(anchor['block_number'])<int(receipt['block_number'])
                or anchor['block_number']==receipt['block_number'] and anchor['block_hash']!=receipt['block_hash']):
            raise DexExecutorError('La parada confirmada no conserva la posición propia del bot')
        result.append({'kind':kind,'id':row.id,'payload_digest':row.payload_digest,'state_digest':row.state_digest})
        current+=1;operator_ref=None
    return result


def review_material(db:Session,previous_run_id:str,*,anchor:dict,expires_at:int,at:datetime,
                    include_resolutions:bool=False,include_external:bool=False)->tuple[dict,PolicyLimits]:
    """Reconstruct a historical proposal; later trades cannot rewrite its past."""
    at=_time(at);run,terms,state,archive=bots.run_record(db,str(UUID(previous_run_id)))
    if include_external and (not include_resolutions or db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<78):
        raise DexExecutorError('La renovación externa requiere su perfil y bóveda actualizados')
    if (terms['schema']in(*EXTERNAL_LONG_ENGINES,*EXTERNAL_DCA_ENGINES))and not include_external:
        raise DexExecutorError('La renovación no puede omitir la revisión externa del bot')
    parent,binding,_=bindings.binding_record(db,run.binding_id)
    if run.status!='STOPPED' or at<=run.updated_at:raise DexExecutorError('Detén y revisa el último permiso antes de renovar')
    if db.scalar(select(RealDexBotRun.id).where(RealDexBotRun.binding_id==parent.id,RealDexBotRun.id!=run.id,
            RealDexBotRun.created_at>=run.created_at,RealDexBotRun.created_at<=at).limit(1)) is not None:
        raise DexExecutorError('La revisión debe partir del último permiso del bot')
    grant_row,grant,_=operator.grant_record(db,parent.grant_id)
    root_binding=parent;root_grant_row,root_grant=grant_row,grant
    carried={key:0 for key in ('buy_spent','entries','realized_loss','acquired_wbnb','gas_spent','last_entry_at')}
    if binding['schema'] in SUCCESSOR_BINDINGS:
        from app.portfolio.dex_controls import control_record
        prior=control_record(db,binding['rollover_id'])[1]['bot_rollover']
        root_binding,_,_=bindings.binding_record(db,binding['root_binding_id'])
        root_grant_row,root_grant,_=operator.grant_record(db,root_binding.grant_id)
        carried={key:int(value) for key,value in prior['consumed'].items()}
        if int(grant['gas_budget_wei'])!=int(root_grant['gas_budget_wei'])-carried['gas_spent']:
            raise DexExecutorError('El gas del sucesor no conserva el presupuesto original')
    root=PolicyLimits.parse(root_grant['limits'])
    settled=_settled_at(db,parent.grant_id,at,include_resolutions=include_resolutions,include_external=include_external)
    references=None
    if include_resolutions:
        from app.portfolio.dex_operator_outcomes import outcome_witnesses
        references=outcome_witnesses(db,[row.id for _,row,_,_,_ in settled],as_of=at,include_external=include_external)
    own=[row.id for _,row,_,_,decision in settled if decision is not None and decision.run_id==run.id]
    position_args={}
    if include_resolutions:position_args['resolved_calls']=outcome_witnesses(db,own,as_of=at,include_external=include_external)
    if include_external:position_args['include_external']=True
    position,entry=bots._position(db,run,terms,own,at,**position_args)
    stops=_policy_stops(db,parent,grant,position,anchor,settled,at,include_resolutions=include_resolutions,include_external=include_external)
    if terms['schema'] in DCA_ENGINES:
        from app.portfolio.dex_dca_journal import _ordered as ordered_predecessors
    else:
        from app.portfolio.dex_bot_continuity import ordered_predecessors
    ancestors=set(ordered_predecessors(db,run.id))
    if any(decision is not None and decision.run_id not in ancestors for _,_,_,_,decision in settled):
        raise DexExecutorError('Una operación no pertenece al linaje de este bot')
    if include_resolutions:
        from app.portfolio.dex_operator_outcomes import operator_outcome
        spent=sum(operator_outcome(db,row.id,as_of=at,include_external=include_external).gas_fee_wei for _,row,_,_,_ in settled)
    else:spent=sum(int(event['receipt']['gas_fee_wei']) for _,_,_,event,_ in settled)
    remaining=int(grant['gas_budget_wei'])-spent
    if remaining<STOP_GAS_LIMIT*root.max_gas_price:raise DexExecutorError('El gas restante no cubre la reserva STOP')
    consumed={key:carried[key]+position[key] for key in ('buy_spent','entries','realized_loss')}
    # CONFIGURE seeds acquired with the position already owned. Count only
    # purchases made under this grant; carrying a position is not a new buy.
    acquired=position['acquired_wbnb']-int(grant['anchor']['counters']['acquired_wbnb'])
    if acquired<0:raise DexExecutorError('El consumo adquirido retrocede respecto al ancla')
    consumed['acquired_wbnb']=carried['acquired_wbnb']+acquired
    consumed['last_entry_at']=max(carried['last_entry_at'],position['last_entry_at'])
    consumed['gas_spent']=carried['gas_spent']+spent
    limits=remaining_limits(root,consumed,owned_wbnb=position['owned_wbnb'],expires_at=expires_at,at=at)
    cursor=archive['cursor'];high=closing=None
    if position['owned_wbnb'] and terms['schema'] not in DCA_ENGINES:
        if entry is None:raise DexExecutorError('La posición no tiene una entrada verificada')
        with localcontext() as context:
            context.prec=300
            average=Decimal(position['cost_usdt'])/Decimal(position['owned_wbnb'])
            high=str(max(average,Decimal(cursor.trailing_high) if cursor.trailing_high is not None else average))
        closing=cursor.closing_reason
    material={'schema':ROLLOVER_SCHEMA,'root_binding_id':root_binding.id,'previous_binding_id':parent.id,
        'previous_run_id':run.id,'previous_payload_digest':run.payload_digest,'previous_state_digest':run.state_digest,
        'previous_binding_digest':parent.payload_digest,'root_grant_digest':root_grant_row.payload_digest,
        'settled_ids':[row.id for _,row,_,_,_ in settled],'policy_stops':stops,
        'consumed':{key:str(value) for key,value in consumed.items()},'gas_remaining_wei':str(remaining),
        'position':{key:str(position[key]) for key in ('owned_wbnb','cost_usdt')},
        'cursor':{'entry_candle_t':entry,'trailing_high':high,'closing_reason':closing,
            'last_candle_t':cursor.last_candle_t,'last_checked_at':cursor.last_checked_at.isoformat()}}
    if terms['schema'] in DCA_ENGINES:
        from app.portfolio.dex_dca_journal import _closed_attempts,_facts_digest
        options={'engine':DCA_EXTERNAL_POLICY if include_external else DCA_POLICY_OUTCOME} if include_resolutions else {}
        _,attempts=_closed_attempts(db,(run,terms,state,archive),anchor=anchor,at=at,**options)
        material['schema']=EXTERNAL_DCA_ROLLOVER if include_external else RECOVERED_DCA_ROLLOVER if include_resolutions else 'orbis.dex.bot.dca.policy-rollover.v1'
        material['cursor']={'reference':cursor.reference,'last_candle_t':cursor.last_candle_t,
            'last_checked_at':cursor.last_checked_at.isoformat()}
        material['dca']={**terms['dca'],'facts_digest':_facts_digest(attempts)}
    elif include_resolutions:material['schema']=EXTERNAL_LONG_ROLLOVER if include_external else RECOVERED_LONG_ROLLOVER
    if include_resolutions:material['resolved_calls']=references
    return material,limits


def validate_control(db:Session,row,terms:dict)->None:
    material,limits=review_material(db,terms['bot_rollover']['previous_run_id'],anchor=terms['anchor'],
        expires_at=terms['limits']['expires_at'],at=row.created_at,include_resolutions=terms['schema']in(RECOVERED_CONTROL,EXTERNAL_CONTROL),
        include_external=terms['schema']==EXTERNAL_CONTROL)
    parent,binding,_=bindings.binding_record(db,material['previous_binding_id'])
    if (terms['bot_rollover']!=material or terms['limits']!=limits.as_dict() or row.account_id!=parent.account_id
            or row.deployment_id!=parent.deployment_id or terms['wallet_digest']!=binding['wallet_digest']):
        raise DexExecutorError('La renovación no conserva la autorización y consumos del bot')


def require_current_review(db:Session,terms:dict,now:datetime)->None:
    """Historical validity is not authority to configure after another rearm."""
    material,limits=review_material(db,terms['bot_rollover']['previous_run_id'],anchor=terms['anchor'],
        expires_at=terms['limits']['expires_at'],at=_time(now),include_resolutions=terms['schema']in(RECOVERED_CONTROL,EXTERNAL_CONTROL),
        include_external=terms['schema']==EXTERNAL_CONTROL)
    parent,_,_=bindings.binding_record(db,material['previous_binding_id'])
    if (material!=terms['bot_rollover'] or limits.as_dict()!=terms['limits']
            or db.scalar(select(RealDexBotBinding.id).where(RealDexBotBinding.deployment_id==parent.deployment_id,
                RealDexBotBinding.id!=parent.id,RealDexBotBinding.status!='STOPPED').limit(1)) is not None):
        raise DexExecutorError('La asignación o los consumos del bot cambiaron desde la revisión')


def replay_rollover(db:Session,*,identifier:str,account_id:str,previous_run_id:str,wallet_address:str,
                    operator_address:str,expires_at:int,gas:str,gas_price:str,max_gas_bnb:str,now:datetime)->dict|None:
    from app.portfolio import dex_controls as controls
    identifier=str(UUID(identifier));wallet=canonical_wallet_address(wallet_address)
    if db.get(RealDexControlIntent,identifier) is None:return None
    row,terms,state=controls.control_record(db,identifier)
    if (row.account_id!=str(UUID(account_id)) or terms['schema'] not in ROLLOVER_CONTROLS
            or terms['wallet_digest']!=wallet_identity(db,'wallet',wallet)
            or terms['operator_ref']!=controls._identity(db,'operator',canonical_wallet_address(operator_address))
            or terms['bot_rollover']['previous_run_id']!=str(UUID(previous_run_id))
            or terms['limits']['expires_at']!=expires_at or terms['gas']!=gas or terms['gas_price']!=gas_price
            or terms['max_gas_wei']!=str(exact_units(max_gas_bnb,18))):
        raise DexExecutorError('El ID de renovación ya se usó con otros datos')
    return {**controls._public(row,terms,state,_time(now)),
        'transaction':controls._compile(terms,wallet,operator_address,now=row.created_at)}


def prepare_rollover(db:Session,*,identifier:str,account_id:str,previous_run_id:str,wallet_address:str,
                     operator_address:str,snapshot,expires_at:int,gas:str,gas_price:str,max_gas_bnb:str,now:datetime)->dict:
    from app.portfolio import dex_controls as controls
    now=_time(now);account_id=str(UUID(account_id));wallet=canonical_wallet_address(wallet_address)
    # Financial outcome capture pins a SQL read snapshot. Own the complete
    # writer transaction before reading it, and compose the control writer
    # without a second BEGIN or a rollback of authenticated inputs.
    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    replay=replay_rollover(db,identifier=identifier,account_id=account_id,previous_run_id=previous_run_id,
        wallet_address=wallet,operator_address=operator_address,expires_at=expires_at,gas=gas,gas_price=gas_price,
        max_gas_bnb=max_gas_bnb,now=now)
    if replay is not None:return replay
    run,run_terms,_,_=bots.run_record(db,previous_run_id);parent,_,_=bindings.binding_record(db,run.binding_id)
    if run.account_id!=account_id:raise DexExecutorError('El bot pertenece a otra cuenta REAL')
    _context(db,parent.deployment_id,account_id,wallet,snapshot,now)
    if snapshot.finalized is not True or not snapshot.position_covered:
        raise DexExecutorError('Revisa una posición cubierta en un bloque finalizado')
    from app.portfolio.dex_operator_outcomes import operator_outcome
    facts=[operator_outcome(db,item,as_of=now,include_external=True)for item in db.scalars(
        select(RealDexOperatorIntent.id).where(RealDexOperatorIntent.grant_id==parent.grant_id,RealDexOperatorIntent.created_at<=now))]
    external=run_terms['schema']in(*EXTERNAL_LONG_ENGINES,*EXTERNAL_DCA_ENGINES)or any(item.external_witness_json is not None for item in facts)
    recovered=external or run_terms['schema'] in {LONG_POLICY_OUTCOME,DCA_POLICY_OUTCOME,'orbis.dex.bot.long.v5','orbis.dex.bot.dca.v2'}
    recovered=recovered or any(item.resolution_id is not None for item in facts)
    material,limits=review_material(db,previous_run_id,anchor=_anchor(db,snapshot),expires_at=expires_at,at=now,
        include_resolutions=recovered,include_external=external)
    return controls.prepare_control(db,identifier=identifier,account_id=account_id,deployment_id=parent.deployment_id,
        wallet_address=wallet,action='CONFIGURE',operator=operator_address,limits=limits.as_dict(),snapshot=snapshot,
        gas=gas,gas_price=gas_price,max_gas_bnb=max_gas_bnb,now=now,bot_rollover=material,begin=False)


def successor_id(control_id:str)->str:
    return str(uuid5(UUID(control_id),'orbis-dex-bot-rollover-binding-v1'))


def reset_counters(control:dict,receipt:dict)->dict:
    position=control['bot_rollover']['position']
    return {'policy_id':receipt['new_policy_id'],'next_nonce':'0','buy_spent':'0','entries':'0',
        'owned_wbnb':position['owned_wbnb'],'cost_usdt':position['cost_usdt'],'realized_loss':'0',
        'last_entry_at':'0','acquired_wbnb':position['owned_wbnb']}


def validate_successor(db:Session,row,terms:dict,grant:dict)->None:
    from app.portfolio import dex_controls as controls
    control_row,control,state=controls.control_record(db,str(UUID(terms['rollover_id'])))
    expected=EXTERNAL_CONTROL if terms['schema']==EXTERNAL_BINDING else RECOVERED_CONTROL if terms['schema']==RECOVERED_BINDING else CONTROL_SCHEMA
    if control['schema']!=expected or control_row.status!='FINALIZED' or state['receipt'] is None:
        raise DexExecutorError('El sucesor requiere un recibo final de renovación')
    material=control['bot_rollover'];old,previous,_=bindings.binding_record(db,material['previous_binding_id'])
    receipt=state['receipt'];anchor=grant['anchor']
    if (row.id!=successor_id(control_row.id) or row.account_id!=old.account_id or row.deployment_id!=old.deployment_id
            or terms['root_binding_id']!=material['root_binding_id'] or terms['origin']!=previous['origin']
            or terms['wallet_digest']!=previous['wallet_digest'] or row.created_at<control_row.updated_at
            or grant['limits']!=control['limits'] or grant['gas_budget_wei']!=material['gas_remaining_wei']
            or anchor['counters']!=reset_counters(control,receipt) or anchor['operator_ref']!=control['operator_ref']
            or int(anchor['block_number'])<int(receipt['block_number'])
            or int(anchor['block_number'])==int(receipt['block_number']) and anchor['block_hash']!=receipt['block_hash']
            or old.status!='STOPPED' or old.updated_at>row.created_at
            or operator.grant_record(db,old.grant_id)[0].status!='STOPPED'):
        raise DexExecutorError('El sucesor no conserva la posición, cuotas o recibo de la renovación')


def apply_rollover(db:Session,*,identifier:str,account_id:str,wallet_address:str,snapshot,now:datetime)->dict:
    """One receipt → one bounded grant/binding. Never connects or starts a bot."""
    from app.portfolio import dex_controls as controls
    now=_time(now);identifier=str(UUID(identifier));account_id=str(UUID(account_id))
    wallet=canonical_wallet_address(wallet_address);new_id=successor_id(identifier)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    existing=db.get(RealDexBotBinding,new_id)
    if existing is not None:
        row,terms,_=bindings.binding_record(db,new_id)
        if row.account_id!=account_id or terms['wallet_digest']!=wallet_identity(db,'wallet',wallet):
            raise DexExecutorError('El sucesor pertenece a otra cuenta o wallet')
        return {**bindings.binding_view(db,new_id,now=now),'replayed':True}
    control_row,control,state=controls.control_record(db,identifier)
    if (control['schema'] not in ROLLOVER_CONTROLS or control_row.status!='FINALIZED' or state['receipt'] is None
            or control_row.account_id!=account_id or control['wallet_digest']!=wallet_identity(db,'wallet',wallet)):
        raise DexExecutorError('Confirma el recibo original de la política renovada antes de continuar')
    require_current_review(db,control,now)
    material=control['bot_rollover'];parent,previous,_=bindings.binding_record(db,material['previous_binding_id'])
    _context(db,parent.deployment_id,account_id,wallet,snapshot,now)
    anchor=_anchor(db,snapshot);receipt=state['receipt']
    if (snapshot.finalized is not True or not snapshot.position_covered or snapshot.policy_limits is None
            or snapshot.policy_limits.as_dict()!=control['limits'] or anchor['counters']!=reset_counters(control,receipt)
            or anchor['operator_ref']!=control['operator_ref'] or now<control_row.updated_at
            or snapshot.block_number<int(receipt['block_number'])
            or snapshot.block_number==int(receipt['block_number']) and snapshot.block_hash!=receipt['block_hash']):
        raise DexExecutorError('La política o posición cambió después de confirmar la renovación')
    bindings._stop_binding(db,parent.id,account_id=account_id,now=now)
    grant_id=str(uuid5(UUID(new_id),'orbis-dex-operator-grant-v1'))
    operator.create_grant(db,identifier=grant_id,account_id=account_id,deployment_id=parent.deployment_id,wallet_address=wallet,
        snapshot=snapshot,gas_budget_wei=int(material['gas_remaining_wei']),now=now,commit=False,begin=False)
    request=bindings._request_from_review(account_id,parent.deployment_id,previous['origin']['review'],material['gas_remaining_wei'])
    request.update(root_binding_id=material['root_binding_id'],rollover_id=identifier)
    terms={'schema':EXTERNAL_BINDING if control['schema']==EXTERNAL_CONTROL else RECOVERED_BINDING if control['schema']==RECOVERED_CONTROL else 'orbis.dex.bot.binding.v2','id':new_id,'account_id':account_id,'deployment_id':parent.deployment_id,
        'grant_id':grant_id,'wallet_digest':previous['wallet_digest'],'origin':previous['origin'],'request':request,
        'request_digest':content_hash(request),'root_binding_id':material['root_binding_id'],'rollover_id':identifier}
    new_state={'status':'READY','history':[{'status':'READY','at':now.isoformat()}]}
    raw,digest=_encoded(terms);state_raw,state_digest=_encoded(new_state)
    db.add(RealDexBotBinding(id=new_id,account_id=account_id,deployment_id=parent.deployment_id,grant_id=grant_id,
        payload_json=raw,payload_digest=digest,state_json=state_raw,state_digest=state_digest,status='READY',created_at=now,updated_at=now))
    db.flush();result=bindings.binding_view(db,new_id,now=now);db.commit()
    return {**result,'replayed':False}


def resume_rollover_material(db:Session,previous:tuple,*,binding:dict,grant:dict,anchor:dict,
                             seed:list[dict],at:datetime,child_id:str,include_resolutions:bool=False,include_external:bool=False)->tuple[dict,DexBotCursor]:
    """Carry strategy state across an evidenced reset, not its physical counters."""
    from app.portfolio.dex_bot_continuity import resume_material
    from app.portfolio import dex_controls as controls
    at=_time(at);row,terms,state,archive=previous
    if row.binding_id==binding['id']:
        return resume_material(db,previous,anchor=anchor,seed=seed,at=at,child_id=child_id,
            **({'include_resolutions':True,'include_external':include_external} if include_resolutions else {}))
    _,control,_=controls.control_record(db,binding['rollover_id']);material=control['bot_rollover']
    if (row.id!=material['previous_run_id'] or row.binding_id!=material['previous_binding_id'] or row.status!='STOPPED'
            or row.payload_digest!=material['previous_payload_digest'] or row.state_digest!=material['previous_state_digest']
            or at<=row.updated_at or anchor['counters']!=grant['anchor']['counters']
            or anchor['operator_ref']!=grant['anchor']['operator_ref']
            or db.scalar(select(RealDexBotRun.id).where(RealDexBotRun.binding_id==binding['id'],
                RealDexBotRun.id!=child_id,RealDexBotRun.created_at<=at).limit(1)) is not None
            or bots._settled(db,row.grant_id,**({'include_resolutions':True,'as_of':at,'include_external':include_external} if include_resolutions else {}))!=material['settled_ids']):
        raise DexExecutorError('La continuidad no corresponde al cierre y recibo de la política anterior')
    references=None
    if include_resolutions:
        from app.portfolio.dex_operator_outcomes import outcome_witnesses
        references=outcome_witnesses(db,material['settled_ids'],as_of=at,include_external=include_external)
        if references!=material.get('resolved_calls',[]):raise DexExecutorError('Las resoluciones cambiaron desde la renovación')
    previous_bars=[bar.json() for bar in archive['bars']]
    if len(seed)<len(previous_bars) or seed[:len(previous_bars)]!=previous_bars:
        raise DexExecutorError('La recuperación debe conservar el histórico causal anterior')
    cursor=material['cursor'];owned=int(material['position']['owned_wbnb'])
    initial=DexBotCursor(seed[-1]['t'],at,cursor['entry_candle_t'] if owned else None,
        cursor['trailing_high'] if owned else None,cursor['closing_reason'] if owned else None)
    result={'previous_run_id':row.id,'previous_payload_digest':row.payload_digest,'previous_state_digest':row.state_digest,
        'settled_ids':material['settled_ids'],'initial_cursor':bots._cursor_json(initial),
        'observation_gap':{'from':cursor['last_checked_at'],'to':at.isoformat()}}
    if include_resolutions:result['resolved_calls']=references
    return result,initial
