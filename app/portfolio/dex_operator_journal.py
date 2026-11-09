"""Durable gas grants and one-shot operator outbox, without keys or raw RLP.

The outbox is a kernel dependency; LAB/PAPER bot activation is a separate gate.
All RPC observations arrive before BEGIN IMMEDIATE. No network runs in here.
"""
from datetime import datetime, timedelta
import hashlib
import hmac
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError, ARTIFACT_SHA256, PROTOCOL
from app.live_execution.dex_policy import PolicyLimits
from app.live_execution.dex_trade import DexTradePlan, DexStopPlan, MAX_GAS_BUDGET, STOP_GAS_LIMIT
from app.live_execution.dex_operator import DexSignature, verify_operator_signature
from app.portfolio.models import RealDexOperatorGrant, RealDexOperatorIntent
from app.portfolio.dex_deployments import deployment_record, _identity as deployment_identity, _hex_ref
from app.portfolio.dex_controls import _context, _anchor, _validate_anchor
from app.portfolio.wallet_funding import _encoded, _decoded, _uint, _time
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address


INTENT_STATES = {'RESERVED','SIGNED','BROADCAST_ATTEMPTED','FINALIZED','REVERTED','NONCE_OBSERVED','ABANDONED_UNSENT'}


def _identity(db: Session, kind: str, value: str) -> str:
    if kind == 'wallet':return deployment_identity(db, kind, value)
    return hmac.new(_salt(db), f'dex-operator-v1:{kind}:0x38:{value.lower()}'.encode(), hashlib.sha256).hexdigest()


def _history(row, state: dict, transitions: dict, first: str) -> list[str]:
    if (not isinstance(state['history'], list) or not 1 <= len(state['history']) <= 6
            or any(not isinstance(event, dict) or set(event) != {'status','at'} for event in state['history'])):raise ValueError('history')
    statuses = [event['status'] for event in state['history']]
    stamps = [_time(datetime.fromisoformat(event['at'])) for event in state['history']]
    if (state['status'] != row.status or statuses[0] != first or statuses[-1] != row.status
            or len(statuses) != len(set(statuses)) or stamps != sorted(stamps)
            or stamps[0] != row.created_at or stamps[-1] != row.updated_at
            or any(stamp.isoformat() != event['at'] for stamp,event in zip(stamps,state['history']))
            or any(right not in transitions.get(left,set()) for left,right in zip(statuses,statuses[1:]))):raise ValueError('state history')
    return statuses


def _schema(db: Session) -> None:
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() < 61:
        raise DexExecutorError('Actualiza la bóveda antes de usar el diario del operador')


def grant_record(db: Session, identifier: str) -> tuple[RealDexOperatorGrant,dict,dict]:
    _schema(db)
    row = db.get(RealDexOperatorGrant, str(UUID(identifier)))
    if row is None:raise DexExecutorError('Concesión del operador no encontrada')
    terms, state = _decoded(row.payload_json,row.payload_digest), _decoded(row.state_json,row.state_digest)
    try:
        if (set(terms) != {'schema','id','account_id','deployment_id','wallet_digest','operator_digest','policy_id','limits',
                          'gas_budget_wei','stop_reserve_wei','anchor','artifact_sha256','protocol'}
                or terms['schema'] != 'orbis.dex.operator.grant.v1' or terms['id'] != row.id or terms['account_id'] != row.account_id
                or terms['deployment_id'] != row.deployment_id or terms['policy_id'] != row.policy_id
                or terms['artifact_sha256'] != ARTIFACT_SHA256 or terms['protocol'] != PROTOCOL
                or not _hex_ref(terms['wallet_digest']) or not _hex_ref(terms['operator_digest'])
                or row.status not in {'ACTIVE','STOPPED'} or set(state) != {'status','history'}):raise ValueError('grant identity')
        parent, deployment, proof = deployment_record(db,row.deployment_id)
        if (parent.account_id != row.account_id or parent.wallet_digest != terms['wallet_digest']
                or not (parent.status == 'FINALIZED' or proof['code_observation'] is not None)):raise ValueError('deployment')
        numbers = _validate_anchor(terms['anchor'], deployment['nonce'], row.created_at)
        limits = PolicyLimits.parse(terms['limits'])
        limits.validate_at(int(row.created_at.timestamp()), numbers['owned_wbnb'])
        budget = _uint(terms['gas_budget_wei'],'Gas de sesión',maximum=MAX_GAS_BUDGET)
        if (limits.as_dict() != terms['limits'] or terms['policy_id'] != terms['anchor']['counters']['policy_id']
                or numbers['policy_id'] == 0 or int(terms['stop_reserve_wei']) != STOP_GAS_LIMIT*limits.max_gas_price
                or not 0 < _uint(terms['stop_reserve_wei'],'Reserva STOP') <= budget):raise ValueError('gas/policy')
        _history(row,state,{'ACTIVE':{'STOPPED'}},'ACTIVE')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('La concesión del operador DEX no es coherente') from None
    return row,terms,state


def _recipe(db: Session, plan: DexTradePlan | DexStopPlan) -> dict:
    state = plan.snapshot
    return {'action': ('BUY' if plan.buy else 'SELL') if type(plan) is DexTradePlan else 'STOP',
        'wallet_digest':_identity(db,'wallet',state.operator), 'owner_digest':_identity(db,'wallet',state.owner),
        'creation_nonce':str(plan.creation_nonce), 'policy_id':str(state.policy_id), 'contract_nonce':str(state.next_nonce),
        'nonce':str(plan.operator_nonce), 'gas':str(plan.gas), 'gas_price':str(plan.gas_price), 'max_gas_wei':str(plan.max_gas_wei),
        'operator_balance':str(plan.operator_balance), 'anchor':_anchor(db,state),
        'trade': {name:str(getattr(plan,name)) for name in ('amount','twap_out','quote_out','minimum','deadline')}
                 | {'slippage_bps':plan.slippage_bps} if type(plan) is DexTradePlan else None}


def _validate_recipe(terms: dict, grant: dict, created_at: datetime) -> None:
    numbers = _validate_anchor(terms['anchor'], terms['creation_nonce'], created_at)
    limits = PolicyLimits.parse(grant['limits'])
    if (terms['wallet_digest'] != grant['operator_digest'] or terms['owner_digest'] != grant['wallet_digest']
            or terms['policy_id'] != grant['policy_id'] or terms['policy_id'] != terms['anchor']['counters']['policy_id']
            or terms['contract_nonce'] != terms['anchor']['counters']['next_nonce']):raise ValueError('operator/policy')
    _uint(terms['nonce'],'Nonce operador',maximum=2**64-1,positive=False)
    gas = _uint(terms['gas'],'Gas',maximum=2_000_000)
    price = _uint(terms['gas_price'],'Precio gas',maximum=limits.max_gas_price)
    maximum = _uint(terms['max_gas_wei'],'Gas máximo',maximum=MAX_GAS_BUDGET)
    if gas < 21000 or gas*price > maximum or gas*price > _uint(terms['operator_balance'],'Saldo de gas'):raise ValueError('gas')
    limits.validate_at(int(created_at.timestamp()), numbers['owned_wbnb'])
    if terms['action'] == 'STOP':
        if terms['trade'] is not None or gas > STOP_GAS_LIMIT:raise ValueError('STOP')
        return
    trade = terms['trade']
    if terms['action'] not in {'BUY','SELL'} or not isinstance(trade,dict) or set(trade) != {'amount','twap_out','quote_out','minimum','deadline','slippage_bps'}:
        raise ValueError('trade')
    amount = _uint(trade['amount'],'Importe',maximum=2**128-1)
    fair, quote, minimum = (_uint(trade[name],name) for name in ('twap_out','quote_out','minimum'))
    if (type(trade['slippage_bps']) is not int or not 1 <= trade['slippage_bps'] <= 500
            or minimum != max(1,fair*(10000-limits.max_deviation_bps)//10000,quote*(10000-trade['slippage_bps'])//10000)
            or minimum > quote or not int(created_at.timestamp()) < _uint(trade['deadline'],'Plazo') <= limits.expires_at
            or int(trade['deadline']) > int(datetime.fromisoformat(terms['anchor']['observed_at']).timestamp())+120):raise ValueError('quote/deadline')
    if terms['action'] == 'BUY':
        if (not limits.allow_buy or amount > limits.max_buy_per_order or numbers['buy_spent']+amount > limits.buy_budget
                or numbers['entries'] >= limits.max_entries or numbers['cost_usdt']+amount > limits.max_exposure
                or numbers['realized_loss'] >= limits.max_realized_loss or numbers['acquired_wbnb']+quote > limits.max_wbnb_acquired
                or numbers['last_entry_at'] and int(created_at.timestamp()) < numbers['last_entry_at']+limits.cooldown_seconds):raise ValueError('buy limits')
    elif not limits.allow_sell or amount > numbers['owned_wbnb'] or fair > limits.max_exit_notional:raise ValueError('sell limits')


def intent_record(db: Session, identifier: str) -> tuple[RealDexOperatorIntent,dict,dict]:
    _schema(db)
    row = db.get(RealDexOperatorIntent,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Intención del operador no encontrada')
    terms,state = _decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
    try:
        if (set(terms) != {'schema','id','account_id','grant_id','decision_ref','action','wallet_digest','owner_digest','creation_nonce',
                          'policy_id','contract_nonce','nonce','gas','gas_price','max_gas_wei','operator_balance','anchor','trade'}
                or terms['schema'] != 'orbis.dex.operator.intent.v1' or terms['id'] != row.id or terms['account_id'] != row.account_id
                or terms['grant_id'] != row.grant_id or terms['wallet_digest'] != row.wallet_digest or terms['decision_ref'] != row.decision_ref
                or not _hex_ref(row.decision_ref) or row.status not in INTENT_STATES
                or set(state) != {'status','history','tx_ref','receipt','nonce_observation'}):raise ValueError('intent identity')
        parent,grant,_ = grant_record(db,row.grant_id)
        if parent.account_id != row.account_id or row.created_at < parent.created_at:raise ValueError('grant account/clock')
        deployment = deployment_record(db,parent.deployment_id)[1]
        if terms['creation_nonce'] != deployment['nonce']:raise ValueError('creation nonce')
        _validate_recipe(terms,grant,row.created_at)
        statuses = _history(row,state,{'RESERVED':{'SIGNED','ABANDONED_UNSENT','NONCE_OBSERVED'},
            'SIGNED':{'BROADCAST_ATTEMPTED','NONCE_OBSERVED','ABANDONED_UNSENT'},'BROADCAST_ATTEMPTED':{'FINALIZED','REVERTED','NONCE_OBSERVED'},
            'NONCE_OBSERVED':{'FINALIZED','REVERTED'}},'RESERVED')
        if (('SIGNED' in statuses) != (state['tx_ref'] is not None) or state['tx_ref'] is not None and not _hex_ref(state['tx_ref'])):
            raise ValueError('signed hash')
        if row.status in {'FINALIZED','REVERTED'}:
            if state['tx_ref'] is None:raise ValueError('receipt without original hash')
            from app.portfolio.dex_operator_receipt import validate_operator_receipt
            validate_operator_receipt(state['receipt'],terms,grant,row)
        elif state['receipt'] is not None:raise ValueError('unexpected receipt')
        if ('NONCE_OBSERVED' in statuses) != (state['nonce_observation'] is not None):raise ValueError('nonce observation')
        if state['nonce_observation'] is not None:
            value = state['nonce_observation']
            if (set(value) != {'source','operator_nonce','block_number','block_hash','block_at','observed_at'}
                    or value['source'] != 'PUBLIC_RPC_NONCE_OBSERVED'
                    or _uint(value['operator_nonce'],'Nonce observado',maximum=2**64-1) <= int(terms['nonce'])
                    or _uint(value['block_number'],'Bloque') < int(terms['anchor']['block_number'])):raise ValueError('nonce proof')
            from app.live_execution.evm_transaction import word
            word(value['block_hash'])
            at = _time(datetime.fromisoformat(value['block_at']))
            observed = _time(datetime.fromisoformat(value['observed_at']))
            event_at = datetime.fromisoformat(state['history'][statuses.index('NONCE_OBSERVED')]['at'])
            if (not -15 <= (event_at-at).total_seconds() <= 30 or not 0 <= (event_at-observed).total_seconds() <= 30
                    or at.isoformat() != value['block_at'] or observed.isoformat() != value['observed_at']):raise ValueError('nonce clock')
            if state['receipt'] is not None:
                receipt = state['receipt']
                if (int(value['block_number']) < int(receipt['block_number'])
                        or value['block_number'] == receipt['block_number'] and value['block_hash'] != receipt['block_hash']):
                    raise ValueError('nonce observation contradicts receipt')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('El diario del operador DEX no es coherente') from None
    return row,terms,state


def _public(row,terms,state) -> dict:
    return {'id':row.id,'account_id':row.account_id,'grant_id':row.grant_id,'status':row.status,'action':terms['action'],
        'plan_digest':row.payload_digest,'nonce':terms['nonce'],'contract_nonce':terms['contract_nonce'],
        'estimated_gas_wei':str(int(terms['gas'])*int(terms['gas_price'])),'receipt':state['receipt'],
        'nonce_observation':state['nonce_observation'],'automatic_execution':False}


def _call_resolution(db,identifier):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<71:return None
    from app.portfolio.dex_call_resolution import status
    return status(db,'OPERATOR',identifier)


def grant_view(db: Session, identifier: str, *, now: datetime) -> dict:
    row,terms,_ = grant_record(db,identifier)
    reserved = spent = external_extra = 0
    unresolved = resolved = False
    external_pending = external_reviewed = 0
    external_support = db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=76
    for item in db.scalars(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id == row.id)):
        action,recipe,state = intent_record(db,item.id)
        if action.status == 'ABANDONED_UNSENT':continue
        resolution=_call_resolution(db,action.id)
        external=None
        if external_support and resolution is None:
            from app.portfolio.dex_external_observation import status as external_status
            external=external_status(db,'OPERATOR',action.id)
        if resolution is not None:
            actual=int(resolution['receipt']['gas_fee_wei']);spent+=actual;resolved=True
            external_extra+=max(0,actual-int(recipe['gas'])*int(recipe['gas_price']))
        elif external is not None:
            actual=int(external['observation']['receipt']['gas_fee_wei']);spent+=actual
            external_extra+=max(0,actual-int(recipe['gas'])*int(recipe['gas_price']))
            from app.portfolio.dex_external_outcomes import external_outcome
            if external_outcome(db,'OPERATOR',action.id,as_of=now)is None:
                unresolved=True;external_pending+=1
            else:external_reviewed+=1
        elif state['receipt'] is not None:spent += int(state['receipt']['gas_fee_wei'])
        else:
            reserved += int(recipe['gas'])*int(recipe['gas_price'])
            unresolved |= action.status in {'SIGNED','BROADCAST_ATTEMPTED','NONCE_OBSERVED'}
    budget = int(terms['gas_budget_wei'])
    if reserved+spent-external_extra > budget:raise DexExecutorError('El diario supera el presupuesto de gas concedido')
    return {'id':row.id,'account_id':row.account_id,'deployment_id':row.deployment_id,'status':row.status,
        'policy_id':terms['policy_id'],'limits':terms['limits'],'gas_budget_wei':str(budget),
        'gas_reserved_wei':str(reserved),'gas_spent_wei':str(spent),'remaining_gas_wei':str(max(0,budget-reserved-spent)),
        **({'gas_overrun_wei':str(max(0,reserved+spent-budget))} if resolved or external_pending or external_reviewed else {}),
        **({'resolved_outcomes':True} if resolved else {}),
        **({'external_outcomes_pending':external_pending} if external_pending else {}),
        **({'external_outcomes_reviewed':external_reviewed} if external_reviewed else {}),
        'stop_reserve_wei':terms['stop_reserve_wei'],'expired':int(_time(now).timestamp()) >= terms['limits']['expires_at'],
        'unresolved_outcomes':unresolved,'automatic_execution':False}


def create_grant(db: Session, *, identifier: str, account_id: str, deployment_id: str, wallet_address: str,
                 snapshot, gas_budget_wei: int, now: datetime, commit: bool = True,begin:bool=True) -> dict:
    """Create gas authority, optionally within the caller's binding transaction.

    With commit=False the caller owns commit/rollback of this same session.
    This function never signs, performs RPC or runs callbacks.
    """
    now = _time(now);identifier,account_id,deployment_id = [str(UUID(value)) for value in (identifier,account_id,deployment_id)]
    wallet = canonical_wallet_address(wallet_address)
    if type(gas_budget_wei) is not int or not 0 < gas_budget_wei <= MAX_GAS_BUDGET:raise DexExecutorError('Presupuesto de gas inválido')
    if begin:db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    elif not db.in_transaction():raise DexExecutorError('La concesión requiere una transacción del llamante')
    _schema(db)
    _context(db,deployment_id,account_id,wallet,snapshot,now)
    if snapshot.policy_limits is None or snapshot.current_operator_code_empty is not True:
        raise DexExecutorError('La política no tiene un operador válido')
    snapshot.policy_limits.validate_at(int(now.timestamp()),snapshot.owned_wbnb)
    reserve = STOP_GAS_LIMIT*snapshot.policy_limits.max_gas_price
    if gas_budget_wei < reserve:raise DexExecutorError('El presupuesto no cubre la reserva STOP')
    terms = {'schema':'orbis.dex.operator.grant.v1','id':identifier,'account_id':account_id,'deployment_id':deployment_id,
        'wallet_digest':_identity(db,'wallet',wallet),'operator_digest':_identity(db,'wallet',snapshot.operator),
        'policy_id':str(snapshot.policy_id),'limits':snapshot.policy_limits.as_dict(),'gas_budget_wei':str(gas_budget_wei),
        'stop_reserve_wei':str(reserve),'anchor':_anchor(db,snapshot),'artifact_sha256':ARTIFACT_SHA256,'protocol':PROTOCOL}
    prior = db.get(RealDexOperatorGrant,identifier)
    if prior is not None:
        _,saved,_ = grant_record(db,identifier)
        if {k:v for k,v in saved.items() if k != 'anchor'} != {k:v for k,v in terms.items() if k != 'anchor'}:
            raise DexExecutorError('El identificador de concesión ya tiene otros datos')
    else:
        if db.scalar(select(RealDexOperatorGrant.id).where(RealDexOperatorGrant.deployment_id == deployment_id,
                RealDexOperatorGrant.policy_id == terms['policy_id'])):
            raise DexExecutorError('Esta política ya tiene presupuesto; reconectar no lo repone')
        state = {'status':'ACTIVE','history':[{'status':'ACTIVE','at':now.isoformat()}]}
        raw,digest = _encoded(terms);state_raw,state_digest = _encoded(state)
        db.add(RealDexOperatorGrant(id=identifier,account_id=account_id,deployment_id=deployment_id,policy_id=terms['policy_id'],
            payload_json=raw,payload_digest=digest,state_json=state_raw,state_digest=state_digest,status='ACTIVE',created_at=now,updated_at=now))
        db.flush()
    result = grant_view(db,identifier,now=now)
    if commit:db.commit()
    return result


def reserve_intent(db: Session, *, identifier: str, account_id: str, grant_id: str, plan: DexTradePlan | DexStopPlan,
                   decision_ref: str, now: datetime) -> dict:
    now = _time(now);identifier,account_id,grant_id = [str(UUID(value)) for value in (identifier,account_id,grant_id)]
    if type(plan) not in (DexTradePlan,DexStopPlan) or not _hex_ref(decision_ref):raise DexExecutorError('Plan o decisión no válidos')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');_schema(db)
    parent,grant,_ = grant_record(db,grant_id)
    terms = {'schema':'orbis.dex.operator.intent.v1','id':identifier,'account_id':account_id,'grant_id':grant_id,
             'decision_ref':decision_ref,**_recipe(db,plan)}
    if parent.account_id != account_id:raise DexExecutorError('Cuenta de concesión distinta')
    prior = db.get(RealDexOperatorIntent,identifier)
    if prior is not None:
        row,saved,state = intent_record(db,identifier)
        if saved != terms:raise DexExecutorError('El identificador de intención ya tiene otros datos')
        return {**_public(row,saved,state),'reserved':False}
    plan.validate(now)
    if type(plan) is DexTradePlan:
        from app.portfolio.dex_bot_bindings import require_active_binding_decision
        require_active_binding_decision(db,grant_id,decision_ref,recipe=terms,now=now)
    view = grant_view(db,grant_id,now=now)
    if (parent.status != 'ACTIVE' and terms['action'] != 'STOP' or view['expired']
            or grant['limits'] != plan.snapshot.policy_limits.as_dict()
            or view['unresolved_outcomes'] and terms['action'] != 'STOP'):
        raise DexExecutorError('La concesión está detenida, caducada, cambió o tiene resultados por resolver')
    _validate_recipe(terms,grant,now)
    if now < parent.created_at:raise DexExecutorError('La intención precede a su concesión')
    reserve = 0 if terms['action'] == 'STOP' else int(grant['stop_reserve_wei'])
    if int(view['remaining_gas_wei']) < plan.gas*plan.gas_price+reserve or plan.operator_balance < plan.gas*plan.gas_price+reserve:
        raise DexExecutorError('El presupuesto o saldo de gas no cubre la operación y STOP')
    if db.scalar(select(RealDexOperatorIntent.id).where(RealDexOperatorIntent.grant_id == grant_id,
            RealDexOperatorIntent.decision_ref == decision_ref)):
        raise DexExecutorError('Esta decisión ya tiene una intención; no se repite')
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    assert_wallet_nonce_available(db,plan.snapshot.operator,str(plan.operator_nonce))
    state = {'status':'RESERVED','history':[{'status':'RESERVED','at':now.isoformat()}],
             'tx_ref':None,'receipt':None,'nonce_observation':None}
    raw,digest = _encoded(terms);state_raw,state_digest = _encoded(state)
    row = RealDexOperatorIntent(id=identifier,account_id=account_id,grant_id=grant_id,wallet_digest=terms['wallet_digest'],
        decision_ref=decision_ref,payload_json=raw,payload_digest=digest,state_json=state_raw,state_digest=state_digest,
        status='RESERVED',created_at=now,updated_at=now)
    db.add(row);db.flush();intent_record(db,identifier);db.commit()
    return {**_public(row,terms,state),'reserved':True}


def _advance(db: Session, row, state: dict, status: str, now: datetime) -> dict:
    if _call_resolution(db,row.id) is not None:raise DexExecutorError('La llamada DEX ya está resuelta; actualiza su historial')
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=76:
        from app.portfolio.dex_external_observation import require_unobserved
        require_unobserved(db,'OPERATOR',row.id)
    now = _time(now)
    if now < row.updated_at:raise DexExecutorError('El reloj del diario retrocedió')
    row.status = state['status'] = status;row.updated_at = now
    state['history'].append({'status':status,'at':now.isoformat()})
    row.state_json,row.state_digest = _encoded(state);db.flush()
    result = _public(*intent_record(db,row.id));db.commit();return result


def record_signature(db: Session, identifier: str, *, account_id: str, signed: DexSignature,
                     plan: DexTradePlan | DexStopPlan, now: datetime) -> dict:
    now = _time(now);plan.validate(now)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state = intent_record(db,identifier)
    try:
        if row.account_id != account_id or type(signed) is not DexSignature:raise ValueError('account/signature')
        recipe = _recipe(db,plan)
        if any(terms[key] != value for key,value in recipe.items()):raise ValueError('plan changed')
        verify_operator_signature(plan,signed)
        reference = _identity(db,'tx',signed.tx_hash)
        if row.status == 'SIGNED' and state['tx_ref'] == reference:return {**_public(row,terms,state),'signature_recorded':False}
        if row.status != 'RESERVED':raise ValueError('state')
        parent,_,_ = grant_record(db,row.grant_id)
        if parent.status != 'ACTIVE' and terms['action'] != 'STOP':raise ValueError('stopped grant')
        if terms['action']!='STOP':
            from app.portfolio.dex_bot_bindings import require_active_binding_decision
            require_active_binding_decision(db,row.grant_id,terms['decision_ref'],recipe=terms,now=now)
        state['tx_ref'] = reference
    except (ValueError,TypeError,KeyError):
        raise DexExecutorError('La firma no coincide con la reserva; no se permite emitirla') from None
    return {**_advance(db,row,state,'SIGNED',now),'signature_recorded':True}


def mark_broadcast(db: Session, identifier: str, *, account_id: str, plan_digest: str, tx_hash: str, now: datetime) -> dict:
    now = _time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state = intent_record(db,identifier)
    parent,grant,_ = grant_record(db,row.grant_id)
    if (row.account_id != account_id or row.status != 'SIGNED' or row.payload_digest != plan_digest
            or state['tx_ref'] != _identity(db,'tx',tx_hash) or parent.status != 'ACTIVE' and terms['action'] != 'STOP'
            or not 0 <= (now-datetime.fromisoformat(terms['anchor']['observed_at'])).total_seconds() <= 15
            or int(now.timestamp()) >= grant['limits']['expires_at']
            or terms['trade'] is not None and int(now.timestamp()) >= int(terms['trade']['deadline'])):
        raise DexExecutorError('La concesión de emisión ya se usó, cambió o caducó')
    if terms['action']!='STOP':
        from app.portfolio.dex_bot_bindings import require_active_binding_decision
        require_active_binding_decision(db,row.grant_id,terms['decision_ref'],recipe=terms,now=now)
    return _advance(db,row,state,'BROADCAST_ATTEMPTED',now)


def abandon_unsent(db: Session, identifier: str, *, account_id: str, now: datetime) -> dict:
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state = intent_record(db,identifier)
    if row.account_id != account_id or row.status not in {'RESERVED','SIGNED','ABANDONED_UNSENT'}:
        raise DexExecutorError('Sólo una intención sin concesión de emisión puede descartarse sin cotejar')
    if row.status == 'ABANDONED_UNSENT':return _public(row,terms,state)
    return _advance(db,row,state,'ABANDONED_UNSENT',now)


def check_intent(db: Session, identifier: str, *, account_id: str, evidence: dict, now: datetime) -> dict:
    from app.portfolio.dex_operator_receipt import observe_operator_receipt
    now = _time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state = intent_record(db,identifier)
    if row.account_id != account_id or row.status not in {'BROADCAST_ATTEMPTED','NONCE_OBSERVED','FINALIZED','REVERTED'} or not state['tx_ref']:
        raise DexExecutorError('La intención no tiene un envío que cotejar')
    _,grant,_ = grant_record(db,row.grant_id)
    receipt = observe_operator_receipt(db,row,terms,grant,state,evidence,now)
    if state['nonce_observation'] is not None:
        observed = state['nonce_observation']
        if (int(observed['block_number']) < int(receipt['block_number'])
                or observed['block_number'] == receipt['block_number'] and observed['block_hash'] != receipt['block_hash']):
            raise DexExecutorError('El recibo contradice el nonce finalizado observado')
    if row.status in {'FINALIZED','REVERTED'}:
        if {k:v for k,v in receipt.items() if k != 'observation_ref'} != {k:v for k,v in state['receipt'].items() if k != 'observation_ref'}:
            raise DexExecutorError('El recibo original ya tiene otros datos')
        return _public(row,terms,state)
    state['receipt'] = receipt
    if terms['action'] == 'STOP' and receipt['status'] == 'FINALIZED':
        _stop_grant(db,row.grant_id,now)
    return _advance(db,row,state,receipt['status'],now)


def _stop_grant(db: Session, identifier: str, now: datetime) -> None:
    row,_,state = grant_record(db,identifier)
    if row.status == 'STOPPED':return
    now = _time(now)
    if now < row.updated_at:raise DexExecutorError('El reloj de la concesión retrocedió')
    row.status = state['status'] = 'STOPPED';row.updated_at = now
    state['history'].append({'status':'STOPPED','at':now.isoformat()})
    row.state_json,row.state_digest = _encoded(state);db.flush();grant_record(db,row.id)


def stop_grant(db: Session, identifier: str, *, account_id: str, now: datetime) -> dict:
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,_,_ = grant_record(db,identifier)
    if row.account_id != account_id:raise DexExecutorError('Cuenta de concesión distinta')
    _stop_grant(db,identifier,now)
    result = grant_view(db,identifier,now=now);db.commit();return result


def recover_intent(db: Session, identifier: str, *, account_id: str, observation, now: datetime) -> dict:
    from app.live_execution.dex_trade import OperatorNonceObservation
    now = _time(now)
    if type(observation) is not OperatorNonceObservation:raise DexExecutorError('Observación de nonce no verificada')
    snapshot = observation.snapshot;snapshot.require_fresh(now)
    if snapshot.finalized is not True:raise DexExecutorError('La recuperación exige un bloque finalizado')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state = intent_record(db,identifier)
    if (row.account_id != account_id or _identity(db,'wallet',snapshot.owner) != terms['owner_digest']
            or _identity(db,'wallet',observation.operator) != row.wallet_digest
            or type(observation.operator_nonce) is not int or not int(terms['nonce']) < observation.operator_nonce <= 2**64-1
            or snapshot.block_number < int(terms['anchor']['block_number'])):
        raise DexExecutorError('El nonce original no se ha observado consumido en un bloque finalizado')
    if row.status in {'FINALIZED','REVERTED','NONCE_OBSERVED'}:return _public(row,terms,state)
    if row.status not in {'RESERVED','SIGNED','BROADCAST_ATTEMPTED'}:raise DexExecutorError('La intención no necesita recuperación de nonce')
    state['nonce_observation'] = {'source':'PUBLIC_RPC_NONCE_OBSERVED','operator_nonce':str(observation.operator_nonce),
        'block_number':str(snapshot.block_number),'block_hash':snapshot.block_hash,'block_at':snapshot.block_at.isoformat(),
        'observed_at':snapshot.observed_at.isoformat()}
    return _advance(db,row,state,'NONCE_OBSERVED',now)


def assert_operator_nonce_available(db: Session, wallet: str, nonce: str) -> None:
    version = db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    if version>=65:
        from app.portfolio.dex_gas_return import require_available
        require_available(db,wallet,nonce)
    if version < 61:return
    reference = _identity(db,'wallet',canonical_wallet_address(wallet))
    if version>=71:
        from app.portfolio.dex_call_resolution import require_signer_nonce_available
        require_signer_nonce_available(db,wallet,nonce)
    for item in db.scalars(select(RealDexOperatorIntent).where(RealDexOperatorIntent.wallet_digest == reference)):
        row,terms,_ = intent_record(db,item.id)
        if version>=78:
            from app.portfolio.dex_external_outcomes import external_outcome
            if external_outcome(db,'OPERATOR',row.id)is not None:
                if int(nonce)<=int(terms['nonce']):raise DexExecutorError('Este nonce externo del operador ya está consumido')
                continue
        if _call_resolution(db,row.id) is not None:
            if terms['nonce']==nonce:raise DexExecutorError('Este nonce del operador ya tiene una resolución')
            continue
        if row.status in {'RESERVED','SIGNED','BROADCAST_ATTEMPTED'}:
            raise DexExecutorError('Hay una intención del operador pendiente; coteja su resultado antes de otra emisión')
        if row.status in {'FINALIZED','REVERTED','NONCE_OBSERVED'} and terms['nonce'] == nonce:
            raise DexExecutorError('Este nonce del operador ya está consumido')


def verify_all_operator_records(db: Session, *, version: int) -> None:
    names = {row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    if version < 61:
        for name in ('real_dex_operator_grants','real_dex_operator_intents'):
            if name in names and db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one():
                raise DexExecutorError('Una bóveda anterior no admite registros del operador')
        return
    for row in db.scalars(select(RealDexOperatorGrant)):grant_view(db,row.id,now=datetime.now().astimezone())
    nonces = set()
    for row in db.scalars(select(RealDexOperatorIntent)):
        item,terms,_ = intent_record(db,row.id)
        if item.status == 'ABANDONED_UNSENT':continue
        key = (item.wallet_digest,terms['nonce'])
        if key in nonces:raise DexExecutorError('El diario contiene dos intenciones para el mismo nonce del operador')
        nonces.add(key)
