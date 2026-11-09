"""Durable owner-only native returns. No RPC, key access or POST under SQL."""
from datetime import datetime,timedelta
import hashlib,hmac,json
from uuid import UUID

from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_return import GasReturnState,GasReturnPlan,plan_return,verify_return_signature,RETURN_GAS
from app.live_execution.evm_transaction import recover_wallet_transfer,quantity,word
from app.portfolio import dex_bot_bindings as bindings,dex_bot_journal as bots,dex_operator_journal as operator
from app.portfolio.dex_deployments import _identity as wallet_identity,_hex_ref
from app.portfolio.dex_deployment_receipt import finalized_block,validate_archived_receipt
from app.portfolio.models import RealDexGasReturnIntent,RealDexOperatorGrant,RealDexOperatorIntent,RealDexBotRun,RealDexGasFundingIntent
from app.portfolio.wallet_funding import _encoded,_decoded,_time,_uint
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address

SCHEMA='orbis.dex.gas.return.v1'
TRANSITIONS={'PREPARED':{'RESERVED','CANCELLED'},'RESERVED':{'SIGNED','ABANDONED_UNSENT'},
    'SIGNED':{'BROADCAST_ATTEMPTED','ABANDONED_UNSENT'},'BROADCAST_ATTEMPTED':{'FINALIZED','REVERTED'}}
PENDING={'RESERVED','SIGNED','BROADCAST_ATTEMPTED'}


def _identity(db:Session,kind:str,value:str)->str:
    if kind=='wallet':return wallet_identity(db,kind,value)
    return hmac.new(_salt(db),f'dex-gas-return-v1:{kind}:0x38:{value.lower()}'.encode(),hashlib.sha256).hexdigest()


def _schema(db:Session)->None:
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<65:
        raise DexExecutorError('Actualiza la bóveda antes de devolver gas')


def _stopped(db:Session,reference:str,at:datetime)->dict:
    """Historical stopped/settled prefix, never infer past state from a future stop."""
    grants=[];runs=[];intents=[]
    for raw in db.scalars(select(RealDexOperatorGrant).where(RealDexOperatorGrant.created_at<=at)):
        row,terms,_=operator.grant_record(db,raw.id)
        if terms['operator_digest']==reference:grants.append(row.id)
    for raw in db.scalars(select(RealDexBotRun).where(RealDexBotRun.grant_id.in_(grants),RealDexBotRun.created_at<=at)):
        row,_,_,_=bots.run_record(db,raw.id)
        if row.status!='STOPPED' or row.updated_at>at:raise DexExecutorError('Detén y concilia los bots de esta operadora antes de devolver BNB')
        runs.append({'id':row.id,'payload_digest':row.payload_digest,'state_digest':row.state_digest})
    for raw in db.scalars(select(RealDexOperatorIntent).where(RealDexOperatorIntent.wallet_digest==reference,RealDexOperatorIntent.created_at<=at)):
        row,_,_=operator.intent_record(db,raw.id)
        if row.status not in {'FINALIZED','REVERTED','ABANDONED_UNSENT'} or row.updated_at>at:
            raise DexExecutorError('Resuelve el envío original de la operadora antes de devolver BNB')
        intents.append({'id':row.id,'payload_digest':row.payload_digest,'state_digest':row.state_digest})
    return {'runs':sorted(runs,key=lambda item:item['id']),'intents':sorted(intents,key=lambda item:item['id'])}


def _require_funding_idle(db:Session,reference:str)->None:
    # Live gate, not a historical reconstruction from mutable future status.
    # A PREPARED funding request may legitimately be claimed after this review.
    from app.portfolio.dex_gas_funding import funding_record
    for raw in db.scalars(select(RealDexGasFundingIntent)):
        if _decoded(raw.payload_json,raw.payload_digest)['operator_digest']!=reference:continue
        row,_,_=funding_record(db,raw.id)
        from app.portfolio.dex_native_resolution import status
        if status(db,'GAS_FUNDING',row.id) is not None:continue
        if row.status in {'WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED'}:
            raise DexExecutorError('Resuelve la financiación BNB original antes de devolver gas')


def _anchor(state:GasReturnState)->dict:
    return {'block_number':str(state.block_number),'block_hash':state.block_hash,'block_at':state.block_at.isoformat(),
        'observed_at':state.observed_at.isoformat(),'operator_nonce':str(state.operator_nonce),'pending_nonce':str(state.pending_nonce),
        'operator_balance':str(state.operator_balance),'owner_code_empty':state.owner_code_empty,'operator_code_empty':state.operator_code_empty}


def _read_anchor(value:dict,at:datetime)->None:
    keys={'block_number','block_hash','block_at','observed_at','operator_nonce','pending_nonce','operator_balance','owner_code_empty','operator_code_empty'}
    if set(value)!=keys:raise ValueError('return observation')
    # Identity is independently checked against the bound grant; dummy addresses
    # let the typed arithmetic validator check only the sanitized numeric anchor.
    state=GasReturnState('0x'+'11'*20,'0x'+'22'*20,_uint(value['block_number'],'Bloque'),word(value['block_hash']),
        _time(datetime.fromisoformat(value['block_at'])),_time(datetime.fromisoformat(value['observed_at'])),
        _uint(value['operator_balance'],'Saldo BNB',positive=False),_uint(value['operator_nonce'],'Nonce',maximum=2**64-1,positive=False),
        _uint(value['pending_nonce'],'Nonce pendiente',maximum=2**64-1,positive=False),value['owner_code_empty'],value['operator_code_empty'])
    state.require_fresh(at)
    if state.block_at.isoformat()!=value['block_at'] or state.observed_at.isoformat()!=value['observed_at']:raise ValueError('return timestamp')


def return_record(db:Session,identifier:str):
    _schema(db);row=db.get(RealDexGasReturnIntent,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Devolución BNB no encontrada')
    terms,state=_decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
    try:
        keys={'schema','id','account_id','binding_id','grant_id','draft_id','wallet_digest','operator_digest','amount_wei',
            'nonce','gas_price','max_gas_wei','anchor','stopped','expires_at'}
        if (set(terms)!=keys or terms['schema']!=SCHEMA or terms['id']!=row.id or terms['account_id']!=row.account_id
                or terms['binding_id']!=row.binding_id or terms['wallet_digest']!=row.wallet_digest or terms['operator_digest']!=row.operator_digest
                or not _hex_ref(row.wallet_digest) or not _hex_ref(row.operator_digest) or str(UUID(terms['draft_id']))!=terms['draft_id']):raise ValueError('return identity')
        parent,binding,_=bindings.binding_record(db,row.binding_id);_,grant,_=operator.grant_record(db,parent.grant_id)
        if (parent.account_id!=row.account_id or parent.created_at>row.created_at or terms['grant_id']!=parent.grant_id
                or row.wallet_digest!=binding['wallet_digest'] or row.operator_digest!=grant['operator_digest']):raise ValueError('return origin')
        _read_anchor(terms['anchor'],row.created_at)
        amount=_uint(terms['amount_wei'],'Devolución BNB');price=_uint(terms['gas_price'],'Precio gas',maximum=10**12)
        maximum=_uint(terms['max_gas_wei'],'Gas máximo',maximum=10**17)
        if (terms['nonce']!=terms['anchor']['operator_nonce'] or amount+RETURN_GAS*price!=int(terms['anchor']['operator_balance'])
                or RETURN_GAS*price>maximum or terms['expires_at']!=(row.created_at+timedelta(seconds=120)).isoformat()
                or terms['stopped']!=_stopped(db,row.operator_digest,row.created_at)):raise ValueError('return limits/lineage')
        if set(state)!={'status','history','tx_ref','receipt'}:raise ValueError('return state')
        statuses=operator._history(row,state,TRANSITIONS,'PREPARED')
        if ('SIGNED' in statuses)!=(state['tx_ref'] is not None) or state['tx_ref'] is not None and not _hex_ref(state['tx_ref']):raise ValueError('return hash')
        if 'BROADCAST_ATTEMPTED' in statuses and datetime.fromisoformat(state['history'][statuses.index('BROADCAST_ATTEMPTED')]['at'])>=datetime.fromisoformat(terms['expires_at']):raise ValueError('expired return send')
        if row.status in {'FINALIZED','REVERTED'}:
            receipt=state['receipt'];validate_archived_receipt({k:v for k,v in receipt.items() if k!='transferred_wei'},terms,row,creation=False)
            if (receipt['transferred_wei']!=(terms['amount_wei'] if row.status=='FINALIZED' else '0')
                    or receipt['transaction_type']!=0 or receipt['gas_limit']!=str(RETURN_GAS) or receipt['gas_used']!=str(RETURN_GAS)
                    or receipt['fee_cap']!=terms['gas_price'] or int(receipt['block_number'])<int(terms['anchor']['block_number'])):raise ValueError('return receipt')
        elif state['receipt'] is not None:raise ValueError('unsettled return receipt')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('El diario de devolución BNB no es coherente') from None
    return row,terms,state


def _public(row,terms,state,now:datetime)->dict:
    return {'id':row.id,'account_id':row.account_id,'binding_id':row.binding_id,'status':row.status,'plan_digest':row.payload_digest,
        'amount_wei':terms['amount_wei'],'gas_wei':str(RETURN_GAS*int(terms['gas_price'])),'nonce':terms['nonce'],
        'expires_at':terms['expires_at'],'expired':_time(now)>=datetime.fromisoformat(terms['expires_at']),
        'receipt':state['receipt'],'automatic_execution':False}


def view_return(db:Session,identifier:str,*,now:datetime)->dict:
    from app.portfolio.dex_native_resolution import status
    result=_public(*return_record(db,identifier),now);resolved=status(db,'GAS_RETURN',identifier)
    return {**result,**({'resolution':resolved} if resolved else {})}


def require_available(db:Session,operator_address:str,nonce:str|None=None,*,exclude:str|None=None)->None:
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<65:return
    reference=_identity(db,'wallet',canonical_wallet_address(operator_address))
    for raw in db.scalars(select(RealDexGasReturnIntent).where(RealDexGasReturnIntent.operator_digest==reference)):
        if raw.id==exclude:continue
        row,terms,_=return_record(db,raw.id)
        from app.portfolio.dex_native_resolution import status
        if status(db,'GAS_RETURN',row.id) is not None:
            if nonce is not None and terms['nonce']==nonce:raise DexExecutorError('Este nonce ya corresponde a una devolución BNB resuelta')
            continue
        if row.status in PENDING:raise DexExecutorError('Hay una devolución BNB pendiente; comprueba su envío original')
        if nonce is not None and row.status in {'FINALIZED','REVERTED'} and terms['nonce']==nonce:
            raise DexExecutorError('Este nonce ya corresponde a una devolución BNB')


def _scope(db,account,binding_id,wallet,drafts,draft_id,plan,now):
    if type(plan) is not GasReturnPlan:raise DexExecutorError('Revisa la devolución BNB exacta')
    plan.validate(now);parent,binding,_=bindings.binding_record(db,binding_id)
    from app.portfolio.dex_controls import require_settled_policy_controls
    require_settled_policy_controls(db,parent.deployment_id)
    _,grant,_=operator.grant_record(db,parent.grant_id)
    address=drafts.require_verified(identifier=draft_id,account_id=account,wallet_address=wallet,now=now)
    if (parent.account_id!=account or binding['wallet_digest']!=_identity(db,'wallet',wallet)
            or plan.snapshot.owner!=wallet or plan.snapshot.operator!=address
            or grant['operator_digest']!=_identity(db,'wallet',address)):raise DexExecutorError('La devolución BNB pertenece a otra wallet u operadora')
    _require_funding_idle(db,grant['operator_digest'])
    return parent


def prepare_return(db,*,identifier,account_id,binding_id,draft_id,wallet_address,drafts,plan,now):
    now=_time(now);identifier,account_id,binding_id,draft_id=map(lambda value:str(UUID(value)),(identifier,account_id,binding_id,draft_id))
    wallet=canonical_wallet_address(wallet_address);db.connection().exec_driver_sql('BEGIN IMMEDIATE');_schema(db)
    prior=db.get(RealDexGasReturnIntent,identifier)
    if prior is not None:
        row,terms,state=return_record(db,identifier)
        if (row.account_id!=account_id or row.binding_id!=binding_id or terms['draft_id']!=draft_id or row.wallet_digest!=_identity(db,'wallet',wallet)
                or terms['amount_wei']!=str(plan.amount_wei) or terms['gas_price']!=str(plan.gas_price) or terms['max_gas_wei']!=str(plan.max_gas_wei)):
            raise DexExecutorError('La solicitud de devolución ya tiene otros datos')
        return {**_public(row,terms,state,now),'replayed':True}
    parent=_scope(db,account_id,binding_id,wallet,drafts,draft_id,plan,now)
    require_available(db,plan.snapshot.operator)
    reference=_identity(db,'wallet',plan.snapshot.operator);stopped=_stopped(db,reference,now)
    terms={'schema':SCHEMA,'id':identifier,'account_id':account_id,'binding_id':binding_id,'grant_id':parent.grant_id,'draft_id':draft_id,
        'wallet_digest':_identity(db,'wallet',wallet),'operator_digest':reference,'amount_wei':str(plan.amount_wei),
        'nonce':str(plan.snapshot.operator_nonce),'gas_price':str(plan.gas_price),'max_gas_wei':str(plan.max_gas_wei),
        'anchor':_anchor(plan.snapshot),'stopped':stopped,'expires_at':(now+timedelta(seconds=120)).isoformat()}
    state={'status':'PREPARED','history':[{'status':'PREPARED','at':now.isoformat()}],'tx_ref':None,'receipt':None}
    raw,digest=_encoded(terms);sraw,sdigest=_encoded(state)
    row=RealDexGasReturnIntent(id=identifier,account_id=account_id,binding_id=binding_id,wallet_digest=terms['wallet_digest'],operator_digest=reference,
        payload_json=raw,payload_digest=digest,state_json=sraw,state_digest=sdigest,status='PREPARED',created_at=now,updated_at=now)
    db.add(row);db.flush();result=view_return(db,identifier,now=now);db.commit();return {**result,'replayed':False}


def _compatible(db,terms,plan,now):
    plan.validate(now);anchor=terms['anchor'];current=plan.snapshot
    if (terms['wallet_digest']!=_identity(db,'wallet',current.owner) or terms['operator_digest']!=_identity(db,'wallet',current.operator)
            or terms['amount_wei']!=str(plan.amount_wei) or terms['gas_price']!=str(plan.gas_price) or terms['max_gas_wei']!=str(plan.max_gas_wei)
            or terms['nonce']!=str(current.operator_nonce) or int(anchor['block_number'])>current.block_number
            or int(anchor['block_number'])==current.block_number and anchor['block_hash']!=current.block_hash):
        raise DexExecutorError('El saldo, nonce o límites de devolución cambiaron; revisa de nuevo')


def _advance(db,row,terms,state,status,now,*,commit=True):
    now=_time(now)
    if now<row.updated_at or status not in TRANSITIONS.get(row.status,set()):raise DexExecutorError('La devolución BNB ya cambió de estado')
    row.status=state['status']=status;row.updated_at=now;state['history'].append({'status':status,'at':now.isoformat()})
    row.state_json,row.state_digest=_encoded(state);db.flush();result=view_return(db,row.id,now=now)
    if commit:db.commit()
    return result


def reserve_return(db,identifier,*,account_id,wallet_address,drafts,draft_id,plan,plan_digest,now):
    now=_time(now);wallet=canonical_wallet_address(wallet_address);db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    row,terms,state=return_record(db,identifier)
    if row.account_id!=account_id or row.wallet_digest!=_identity(db,'wallet',wallet) or row.payload_digest!=plan_digest:raise DexExecutorError('La revisión de devolución no coincide')
    if row.status!='PREPARED':return {**_public(row,terms,state,now),'reserved':False}
    _scope(db,account_id,row.binding_id,wallet,drafts,draft_id,plan,now);_compatible(db,terms,plan,now)
    if now>=datetime.fromisoformat(terms['expires_at']) or terms['stopped']!=_stopped(db,row.operator_digest,now):raise DexExecutorError('La revisión de devolución caducó o cambió el bot')
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    assert_wallet_nonce_available(db,plan.snapshot.operator,terms['nonce'])
    return {**_advance(db,row,terms,state,'RESERVED',now),'reserved':True}


def record_signature(db,identifier,*,account_id,plan,raw,tx_hash,now):
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=return_record(db,identifier)
    if row.account_id!=account_id or row.status!='RESERVED':raise DexExecutorError('La devolución no tiene reserva de firma')
    _compatible(db,terms,plan,now);verify_return_signature(plan,raw,tx_hash)
    state['tx_ref']=_identity(db,'tx',tx_hash)
    return {**_advance(db,row,terms,state,'SIGNED',now),'signature_recorded':True}


def mark_broadcast(db,identifier,*,account_id,plan_digest,tx_hash,now):
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=return_record(db,identifier)
    if (row.account_id!=account_id or row.payload_digest!=plan_digest or row.status!='SIGNED'
            or state['tx_ref']!=_identity(db,'tx',word(tx_hash)) or now>=datetime.fromisoformat(terms['expires_at'])
            or terms['stopped']!=_stopped(db,row.operator_digest,now)):
        raise DexExecutorError('La devolución ya se concedió, caducó o cambió; no se repite')
    _require_funding_idle(db,row.operator_digest)
    return {**_advance(db,row,terms,state,'BROADCAST_ATTEMPTED',now),'broadcast_granted':True}


def check_return(db,identifier,*,account_id,evidence,now,commit=True):
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=return_record(db,identifier)
    if row.account_id!=account_id or row.status not in {'BROADCAST_ATTEMPTED','FINALIZED','REVERTED'}:raise DexExecutorError('Comprueba el envío BNB original')
    from app.portfolio.dex_native_resolution import require_unresolved
    require_unresolved(db,'GAS_RETURN',row.id)
    try:
        raw=json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(raw)>100000 or set(evidence)!={'chain_id','tx_hash','transaction','receipt','block','finalized'}:raise ValueError('return evidence')
        tx_hash=word(evidence['tx_hash']);tx,receipt,block,final=[evidence[key] for key in ('transaction','receipt','block','finalized')]
        if evidence['chain_id']!='0x38' or state['tx_ref']!=_identity(db,'tx',tx_hash):raise ValueError('return original hash')
        owner=canonical_wallet_address(tx['to']);source,kind,gas,cap,_=recover_wallet_transfer(tx,expected_to=owner,expected_value=int(terms['amount_wei']))
        if (_identity(db,'wallet',owner)!=row.wallet_digest or _identity(db,'wallet',source)!=row.operator_digest or kind!=0
                or gas!=RETURN_GAS or cap!=int(terms['gas_price']) or quantity(tx['nonce'])!=int(terms['nonce'])):raise ValueError('return signed scope')
        height,block_hash,at=finalized_block(block,final,created_at=row.created_at,now=now)
        succeeded=quantity(receipt['status'])
        if (height<int(terms['anchor']['block_number']) or word(tx['hash'])!=tx_hash or word(receipt['transactionHash'])!=tx_hash
                or canonical_wallet_address(receipt['from'])!=source or canonical_wallet_address(receipt['to'])!=owner
                or receipt['contractAddress'] is not None or receipt['logs']!=[] or quantity(receipt['type'])!=0
                or quantity(receipt['gasUsed'])!=RETURN_GAS or quantity(receipt['effectiveGasPrice'])!=cap or succeeded not in (0,1)
                or any(quantity(item['blockNumber'])!=height or word(item['blockHash'])!=block_hash for item in (tx,receipt))
                or quantity(tx['transactionIndex'])!=quantity(receipt['transactionIndex'])):raise ValueError('return receipt linkage')
        observed={'status':'FINALIZED' if succeeded else 'REVERTED','source':'WALLET_RPC_OBSERVED','block_number':str(height),'block_hash':block_hash,
            'block_at':at.isoformat(),'transaction_type':0,'gas_limit':str(RETURN_GAS),'gas_used':str(RETURN_GAS),'fee_cap':str(cap),
            'priority_fee':None,'base_fee':None,'effective_gas_price':str(cap),'gas_fee_wei':str(RETURN_GAS*cap),
            'observation_ref':_identity(db,'receipt',raw),'transferred_wei':terms['amount_wei'] if succeeded else '0'}
        if row.status in {'FINALIZED','REVERTED'}:
            if {k:v for k,v in observed.items() if k!='observation_ref'}!={k:v for k,v in state['receipt'].items() if k!='observation_ref'}:raise ValueError('changed receipt')
            return _public(row,terms,state,now)
        state['receipt']=observed;return _advance(db,row,terms,state,observed['status'],now,commit=commit)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,BadSignature,ValidationError):
        raise DexExecutorError('No se ha verificado el recibo original de devolución BNB') from None


def cancel_return(db,identifier,*,account_id,now):
    """Only an unreserved review is user-cancelable; it never exposed bytes."""
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=return_record(db,identifier)
    if row.account_id!=account_id:raise DexExecutorError('La devolución pertenece a otra cuenta')
    if row.status=='CANCELLED':return _public(row,terms,state,now)
    if row.status!='PREPARED':raise DexExecutorError('La devolución reservada requiere conciliación, no cancelación')
    return _advance(db,row,terms,state,'CANCELLED',now)


def abandon_unsent(db,identifier,*,account_id,now):
    """Internal outbox barrier. A later broadcast claim then fails atomically."""
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=return_record(db,identifier)
    if row.account_id!=account_id:raise DexExecutorError('La devolución pertenece a otra cuenta')
    if row.status=='ABANDONED_UNSENT':return _public(row,terms,state,now)
    if row.status not in {'RESERVED','SIGNED'}:raise DexExecutorError('Un envío ya concedido no se abandona ni se repite')
    return _advance(db,row,terms,state,'ABANDONED_UNSENT',now)


def verify_all_returns(db,*,version):
    if version<65:
        names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'real_dex_gas_return_intents' in names and db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_gas_return_intents').scalar_one():
            raise DexExecutorError('Una bóveda anterior no admite devoluciones de gas')
        return
    nonces=set();pending=set()
    for raw in db.scalars(select(RealDexGasReturnIntent)):
        row,terms,_=return_record(db,raw.id)
        if row.status in {'PREPARED','CANCELLED','ABANDONED_UNSENT'}:continue
        key=(row.operator_digest,terms['nonce'])
        if key in nonces or row.status in PENDING and row.operator_digest in pending:raise DexExecutorError('Devolución o nonce BNB duplicado')
        nonces.add(key)
        from app.portfolio.dex_native_resolution import status
        if status(db,'GAS_RETURN',row.id) is not None:continue
        if row.status in PENDING:pending.add(row.operator_digest)
