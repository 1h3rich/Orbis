"""Atomic immutable LAB/PAPER → executor/policy/gas binding, SQLCipher only.

READY is stored provenance, not a running worker or signing permission. Reopen
never starts anything. No private key, address, signed bytes or live tx hash.
"""
from app.live_execution.dex_versions import SUCCESSOR_BINDINGS
from datetime import datetime
from uuid import UUID,uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_origin import PreparedDexOrigin,validate_origin
from app.live_execution.dex_bot_source import exact_units
from app.live_execution.dex_policy import PolicyLimits
from app.live_execution.dex_trade import MAX_GAS_BUDGET
from app.lab.store import content_hash
from app.portfolio import dex_operator_journal as operator
from app.portfolio.dex_controls import _context
from app.portfolio.dex_deployments import _identity as wallet_identity
from app.portfolio.models import RealDexBotBinding
from app.portfolio.wallet_funding import _encoded,_decoded,_time,_uint
from app.portfolio.wallet_proof import canonical_wallet_address


CAPS={'entry_units':('max_buy_per_order',1000),'budget_units':('buy_budget',10000),
      'exposure_units':('max_exposure',10000),'loss_units':('max_realized_loss',10000),
      'wbnb_quota_units':('max_wbnb_acquired',1000000)}


def _schema(db):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<62:
        raise DexExecutorError('Actualiza la bóveda para vincular un bot DEX')


def request_terms(*,account_id:str,deployment_id:str,source_kind:str,paper_session_id:str,entry_usdt:str,budget_usdt:str,
                  exposure_usdt:str,loss_usdt:str,wbnb_quota:str,monthly_usdt:str|None,gas_budget_wei:int) -> dict:
    if source_kind not in ('STRATEGY','DCA'):raise DexExecutorError('Tipo de bot no admitido')
    identifiers=[str(UUID(value)) for value in (account_id,deployment_id,paper_session_id)]
    if type(gas_budget_wei) is not int or not 0<gas_budget_wei<=MAX_GAS_BUDGET:raise DexExecutorError('Presupuesto de gas inválido')
    values=(entry_usdt,budget_usdt,exposure_usdt,loss_usdt,wbnb_quota)
    amounts={key:str(exact_units(value,key,maximum=cap)) for (key,(_,cap)),value in zip(CAPS.items(),values)}
    if int(amounts['entry_units'])>min(int(amounts['budget_units']),int(amounts['exposure_units'])):
        raise DexExecutorError('La entrada supera el presupuesto o la exposición')
    if source_kind=='STRATEGY' and monthly_usdt is not None:raise DexExecutorError('El límite mensual corresponde a DCA')
    monthly=str(exact_units(monthly_usdt,'Límite mensual',maximum=10000)) if source_kind=='DCA' else None
    return {'account_id':identifiers[0],'deployment_id':identifiers[1],'source_kind':source_kind,'session_id':identifiers[2],
            'amounts':amounts,'monthly_units':monthly,'gas_budget_wei':str(gas_budget_wei)}


def _request_from_review(account_id,deployment_id,review,gas_budget):
    return {'account_id':account_id,'deployment_id':deployment_id,'source_kind':review['source_kind'],
        'session_id':review['source']['session_id'],'amounts':review['amounts'],
        'monthly_units':review['dca']['monthly_units'] if review['dca'] else None,'gas_budget_wei':str(gas_budget)}


def _limits(review,limits):
    if set(review['amounts'])!=set(CAPS):raise DexExecutorError('Límites REAL no válidos')
    for key,(field,maximum) in CAPS.items():
        cap=_uint(review['amounts'][key],key,maximum=maximum*10**18)
        if not 0<getattr(limits,field)<=cap:raise DexExecutorError('La política en cadena supera '+key)
    if int(review['amounts']['entry_units'])>min(int(review['amounts']['budget_units']),int(review['amounts']['exposure_units'])):
        raise DexExecutorError('La entrada supera el presupuesto o la exposición')
    if limits.allow_buy is not True or limits.allow_sell is not review['allow_sell']:
        raise DexExecutorError('Los permisos de la política no corresponden al tipo de bot')


def binding_record(db:Session,identifier:str,*,replay:bool=False):
    from app.portfolio.dex_verified_read import read_record
    identifier=str(UUID(identifier));row=db.get(RealDexBotBinding,identifier)
    if row is None:raise DexExecutorError('Vínculo de bot DEX no encontrado')
    terms=_decoded(row.payload_json,row.payload_digest)
    return read_record(db,'binding',identifier,lambda current:_binding_record(db,identifier,replay=replay or current['force_full']),
        warm=identifier if terms.get('schema') in SUCCESSOR_BINDINGS else None,force_full=replay)


def _binding_record(db:Session,identifier:str,*,replay:bool=False):
    _schema(db);row=db.get(RealDexBotBinding,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Vínculo de bot DEX no encontrado')
    terms,state=_decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
    try:
        expected={'schema','id','account_id','deployment_id','grant_id','wallet_digest','origin','request','request_digest'}
        successor=terms.get('schema') in SUCCESSOR_BINDINGS
        from app.live_execution.dex_versions import EXTERNAL_BINDING
        if terms.get('schema')==EXTERNAL_BINDING and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<78:
            raise ValueError('external binding schema')
        if successor:expected=expected|{'rollover_id','root_binding_id'}
        if (set(terms)!=expected or terms['schema'] not in ('orbis.dex.bot.binding.v1',*SUCCESSOR_BINDINGS) or terms['id']!=row.id
                or terms['account_id']!=row.account_id or terms['deployment_id']!=row.deployment_id or terms['grant_id']!=row.grant_id
                or row.grant_id!=str(uuid5(UUID(row.id),'orbis-dex-operator-grant-v1'))):raise ValueError('identity')
        validate_origin(terms['origin'],replay=replay)
        parent,grant,_=operator.grant_record(db,row.grant_id)
        if (parent.account_id!=row.account_id or parent.deployment_id!=row.deployment_id or row.created_at!=parent.created_at
                or terms['wallet_digest']!=grant['wallet_digest']):raise ValueError('grant')
        review=terms['origin']['review']
        expected_request=_request_from_review(row.account_id,row.deployment_id,review,grant['gas_budget_wei'])
        if successor:expected_request.update(rollover_id=terms['rollover_id'],root_binding_id=terms['root_binding_id'])
        if (terms['request']!=expected_request or terms['request_digest']!=content_hash(expected_request)
                or not successor and not datetime.fromisoformat(review['created_at'])<=row.created_at<datetime.fromisoformat(review['expires_at'])):
            raise ValueError('request')
        if successor:
            from app.portfolio.dex_bot_rollover import validate_successor
            validate_successor(db,row,terms,grant)
        else:
            _limits(review,PolicyLimits.parse(grant['limits']))
            if any(int(value) for key,value in grant['anchor']['counters'].items() if key not in ('policy_id','next_nonce')):
                raise ValueError('preexisting execution')
        if set(state)!={'status','history'} or row.status not in ('READY','STOPPED'):raise ValueError('state')
        statuses=operator._history(row,state,{'READY':{'STOPPED'}},'READY')
        if row.status=='STOPPED' and parent.status!='STOPPED':raise ValueError('stopped binding with active grant')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('El vínculo guardado del bot DEX no es coherente') from None
    return row,terms,state


def binding_view(db:Session,identifier:str,*,now:datetime) -> dict:
    from app.portfolio.dex_research import binding_qualification
    row,terms,_=binding_record(db,identifier)
    grant=operator.grant_view(db,row.grant_id,now=now)
    review=terms['origin']['review']
    continuity={}
    if terms['schema'] in SUCCESSOR_BINDINGS:
        from app.portfolio.dex_controls import control_record
        previous=control_record(db,terms['rollover_id'])[1]['bot_rollover']
        continuity={'root_binding_id':terms['root_binding_id'],'renewed_from_binding_id':previous['previous_binding_id'],
            'rollover_id':terms['rollover_id'],'resume_from_run_id':previous['previous_run_id']}
    return {'id':row.id,'account_id':row.account_id,'deployment_id':row.deployment_id,'grant_id':row.grant_id,
        **continuity,
        'research_qualification':binding_qualification(db,identifier),
        'status':row.status,'grant_status':grant['status'],'source_kind':review['source_kind'],'source':review['source'],
        'amounts':review['amounts'],'dca':review['dca'],'effective_limits':grant['limits'],
        'gas':{key:grant[key] for key in ('gas_budget_wei','gas_reserved_wei','gas_spent_wei','remaining_gas_wei','stop_reserve_wei')},
        'expired':grant['expired'],'plan_digest':row.payload_digest,'created_at':row.created_at.isoformat(),
        'execution_enabled':False,'automatic_execution':False,'activation_pending':True}


def replay_binding(db:Session,identifier:str,*,request:dict,wallet_address:str,now:datetime,research_validation_id:str|None=None) -> dict|None:
    _schema(db)
    if db.get(RealDexBotBinding,str(UUID(identifier))) is None:return None
    row,terms,_=binding_record(db,identifier)
    if (terms['request']!=request or terms['wallet_digest']!=wallet_identity(db,'wallet',canonical_wallet_address(wallet_address))):
        raise DexExecutorError('El identificador de vínculo ya tiene otro origen, wallet o límites')
    result=binding_view(db,row.id,now=now)
    proof=result['research_qualification']
    if research_validation_id is not None and (proof is None or proof['evidence']['research']['artifact_id']!=research_validation_id):
        raise DexExecutorError('La solicitud de vínculo ya tiene otra reserva')
    return {**result,'replayed':True}


def create_binding(db:Session,*,identifier:str,account_id:str,deployment_id:str,wallet_address:str,
                   prepared:PreparedDexOrigin,snapshot,gas_budget_wei:int,now:datetime) -> dict:
    now=_time(now);identifier,account_id,deployment_id=[str(UUID(value)) for value in (identifier,account_id,deployment_id)]
    _schema(db)
    if type(prepared) is not PreparedDexOrigin:raise DexExecutorError('Reproduce la preparación desde LAB/PAPER')
    origin=prepared.material(now=now);review=origin['review']
    if "volume_gate" in review['definition'] and prepared.research is None:
        raise DexExecutorError('El DCA de volumen requiere una reserva fuera de muestra verificada')
    wallet=canonical_wallet_address(wallet_address)
    _context(db,deployment_id,account_id,wallet,snapshot,now)
    if snapshot.finalized is not True:raise DexExecutorError('El vínculo exige una política observada en bloque finalizado')
    if snapshot.policy_limits is None:raise DexExecutorError('Configura primero la política del ejecutor')
    _limits(review,snapshot.policy_limits)
    request=_request_from_review(account_id,deployment_id,review,gas_budget_wei)
    validation_id=str(prepared.research.evidence.research.artifact_id) if prepared.research is not None else None
    prior=replay_binding(db,identifier,request=request,wallet_address=wallet,now=now,research_validation_id=validation_id)
    if prior:return prior
    if any(getattr(snapshot,key) for key in ('buy_spent','entries','owned_wbnb','cost_usdt','realized_loss','last_entry_at','acquired_wbnb')):
        raise DexExecutorError('El primer vínculo requiere una política nueva y una posición propia vacía')
    grant_id=str(uuid5(UUID(identifier),'orbis-dex-operator-grant-v1'))
    operator.create_grant(db,identifier=grant_id,account_id=account_id,deployment_id=deployment_id,wallet_address=wallet,
        snapshot=snapshot,gas_budget_wei=gas_budget_wei,now=now,commit=False)
    # create_grant now owns BEGIN IMMEDIATE; repeat after acquiring that lock.
    prior=replay_binding(db,identifier,request=request,wallet_address=wallet,now=now,research_validation_id=validation_id)
    if prior:db.commit();return prior
    if db.scalar(select(RealDexBotBinding.id).where(RealDexBotBinding.deployment_id==deployment_id,RealDexBotBinding.status!='STOPPED')):
        raise DexExecutorError('Este ejecutor ya tiene un vínculo; no se asigna a dos bots')
    terms={'schema':'orbis.dex.bot.binding.v1','id':identifier,'account_id':account_id,'deployment_id':deployment_id,
        'grant_id':grant_id,'wallet_digest':wallet_identity(db,'wallet',wallet),'origin':origin,'request':request,
        'request_digest':content_hash(request)}
    state={'status':'READY','history':[{'status':'READY','at':now.isoformat()}]}
    raw,digest=_encoded(terms);state_raw,state_digest=_encoded(state)
    db.add(RealDexBotBinding(id=identifier,account_id=account_id,deployment_id=deployment_id,grant_id=grant_id,
        payload_json=raw,payload_digest=digest,state_json=state_raw,state_digest=state_digest,status='READY',created_at=now,updated_at=now))
    db.flush()
    if prepared.research is not None:
        from app.portfolio.dex_research import save_binding_qualification
        save_binding_qualification(db,identifier,prepared.research)
    result=binding_view(db,identifier,now=now);db.commit();return {**result,'replayed':False}


def list_bindings(db:Session,account_id:str,*,now:datetime) -> list[dict]:
    _schema(db)
    return [binding_view(db,item,now=now) for item in db.scalars(select(RealDexBotBinding.id).where(
        RealDexBotBinding.account_id==str(UUID(account_id))).order_by(RealDexBotBinding.created_at.desc()).limit(100))]


def _stop_binding(db:Session,identifier:str,*,account_id:str,now:datetime)->None:
    now=_time(now);row,terms,state=binding_record(db,identifier)
    if row.account_id!=account_id:raise DexExecutorError('El vínculo no pertenece a esta cuenta')
    if row.status!='STOPPED':
        if now<row.updated_at:raise DexExecutorError('El reloj del vínculo retrocedió')
        operator._stop_grant(db,row.grant_id,now)
        row.status=state['status']='STOPPED';row.updated_at=now
        state['history'].append({'status':'STOPPED','at':now.isoformat()})
        row.state_json,row.state_digest=_encoded(state);db.flush()


def stop_binding(db:Session,identifier:str,*,account_id:str,now:datetime) -> dict:
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    _stop_binding(db,identifier,account_id=account_id,now=now)
    result=binding_view(db,identifier,now=now);db.commit();return result


def verify_all_bindings(db:Session,*,version:int) -> None:
    if version<62:
        names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'real_dex_bot_bindings' in names and db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_bot_bindings').scalar_one():
            raise DexExecutorError('Una bóveda anterior no admite vínculos de bot DEX')
        return
    live=set()
    for row in db.scalars(select(RealDexBotBinding)):
        binding_record(db,row.id,replay=True)
        if row.status!='STOPPED':
            if row.deployment_id in live:raise DexExecutorError('El ejecutor está asignado a dos bots')
            live.add(row.deployment_id)


def require_active_binding_decision(db:Session,grant_id:str,decision_ref:str,*,recipe:dict|None=None,now:datetime|None=None) -> None:
    """A bound swap must match its latest active, reproducible decision."""
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<62:return
    if db.scalar(select(RealDexBotBinding.id).where(RealDexBotBinding.grant_id==grant_id)):
        if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<64:
            raise DexExecutorError('El vínculo requiere activación del bot y una decisión causal antes de operar')
        from app.portfolio.dex_bot_journal import require_decision
        require_decision(db,grant_id,decision_ref,recipe,now)
