"""Durable native BNB funding, distinct from trading authority and gas spent.

Only the owner wallet may send. No RPC, keys or signing run inside SQL writes.
Unknown outcomes block new wallet requests through the shared nonce guard.
"""
from datetime import datetime
import hashlib,hmac,json,re
from uuid import UUID

from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_funding import FundingState
from app.live_execution.dex_operator_drafts import DexOperatorDrafts
from app.live_execution.dex_trade import MAX_GAS_BUDGET
from app.live_execution.evm_transaction import quantity,word,recover_wallet_transfer
from app.portfolio import dex_bot_bindings as bindings,dex_operator_journal as operator
from app.portfolio.dex_controls import _context,_anchor,_validate_anchor,_expiry,require_settled_policy_controls
from app.portfolio.dex_deployments import deployment_record,_hex_ref,_identity as wallet_identity
from app.portfolio.dex_deployment_receipt import finalized_block,validate_archived_receipt,require_receipt_before_observation
from app.portfolio.models import RealDexGasFundingIntent
from app.portfolio.wallet_funding import _encoded,_decoded,_uint,_time,exact_units
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address

STATES={'PREPARED','WALLET_REQUESTED','HASH_REPORTED','WALLET_REJECTED','FINALIZED','REVERTED','NONCE_OBSERVED'}
TRANSITIONS={'PREPARED':{'WALLET_REQUESTED'},'WALLET_REQUESTED':{'HASH_REPORTED','WALLET_REJECTED','NONCE_OBSERVED'},
    'HASH_REPORTED':{'FINALIZED','REVERTED','NONCE_OBSERVED'},'NONCE_OBSERVED':{'HASH_REPORTED','FINALIZED','REVERTED'}}


def _identity(db:Session,kind:str,value:str)->str:
    if kind=='wallet':return wallet_identity(db,kind,value)
    return hmac.new(_salt(db),f'dex-gas-funding-v1:{kind}:0x38:{value.lower()}'.encode(),hashlib.sha256).hexdigest()


def _schema(db:Session)->None:
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<63:
        raise DexExecutorError('Actualiza la bóveda antes de financiar la operadora')


def funding_record(db:Session,identifier:str):
    _schema(db);row=db.get(RealDexGasFundingIntent,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Envío de gas no encontrado')
    terms,state=_decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
    try:
        keys={'schema','id','account_id','binding_id','draft_id','deployment_id','wallet_digest','operator_digest','policy_id',
            'creation_nonce','amount_wei','operator_balance','owner_balance','remaining_gas_wei','nonce','gas','gas_price',
            'max_gas_wei','anchor','expires_at'}
        if (set(terms)!=keys or terms['schema']!='orbis.dex.gas.funding.v1' or terms['id']!=row.id
                or terms['account_id']!=row.account_id or terms['binding_id']!=row.binding_id
                or terms['wallet_digest']!=row.wallet_digest or not _hex_ref(row.wallet_digest)
                or not _hex_ref(terms['operator_digest']) or str(UUID(terms['draft_id']))!=terms['draft_id']):raise ValueError('identity')
        parent,binding,_=bindings.binding_record(db,row.binding_id)
        _,grant,_=operator.grant_record(db,parent.grant_id)
        deployment=deployment_record(db,parent.deployment_id)[1]
        if (parent.account_id!=row.account_id or parent.created_at>row.created_at
                or terms['deployment_id']!=parent.deployment_id or terms['creation_nonce']!=deployment['nonce']
                or terms['wallet_digest']!=grant['wallet_digest'] or terms['operator_digest']!=grant['operator_digest']
                or terms['policy_id']!=grant['policy_id']):raise ValueError('parent')
        _validate_anchor(terms['anchor'],terms['creation_nonce'],row.created_at)
        if terms['anchor']['counters']['policy_id']!=terms['policy_id'] or terms['anchor']['pending_nonce']!=terms['nonce']:
            raise ValueError('anchor')
        _uint(terms['nonce'],'Nonce',maximum=2**64-1,positive=False)
        gas=_uint(terms['gas'],'Gas',maximum=100000);price=_uint(terms['gas_price'],'Precio gas',maximum=10**12)
        maximum=_uint(terms['max_gas_wei'],'Gas máximo',maximum=MAX_GAS_BUDGET)
        amount=_uint(terms['amount_wei'],'BNB',maximum=MAX_GAS_BUDGET)
        balance=_uint(terms['operator_balance'],'Saldo operadora',positive=False)
        remaining=_uint(terms['remaining_gas_wei'],'Presupuesto restante',maximum=int(grant['gas_budget_wei']))
        owner_balance=_uint(terms['owner_balance'],'Saldo wallet')
        if (gas<21000 or gas*price>maximum or amount+maximum>owner_balance or amount+balance>remaining
                or terms['expires_at']!=_expiry(row.created_at,grant['limits']).isoformat()):raise ValueError('budget')
        if set(state)!={'status','history','tx_ref','receipt','nonce_observation'} or row.status not in STATES:raise ValueError('state')
        statuses=operator._history(row,state,TRANSITIONS,'PREPARED')
        if (('HASH_REPORTED' in statuses)!=(state['tx_ref'] is not None)
                or state['tx_ref'] is not None and not _hex_ref(state['tx_ref'])):raise ValueError('hash')
        if row.status in {'FINALIZED','REVERTED'}:
            if state['tx_ref'] is None:raise ValueError('unhashed receipt')
            receipt=state['receipt']
            if not isinstance(receipt,dict) or 'transferred_wei' not in receipt:raise ValueError('receipt')
            validate_archived_receipt({k:v for k,v in receipt.items() if k!='transferred_wei'},terms,row,creation=False)
            if (int(receipt['gas_limit'])>100000 or receipt['transferred_wei']!=(terms['amount_wei'] if row.status=='FINALIZED' else '0')
                    or int(receipt['block_number'])<int(terms['anchor']['block_number'])):raise ValueError('native amount')
        elif state['receipt'] is not None:raise ValueError('unexpected receipt')
        if ('NONCE_OBSERVED' in statuses)!=(state['nonce_observation'] is not None):raise ValueError('nonce proof')
        if state['nonce_observation'] is not None:
            observed=state['nonce_observation']
            if (set(observed)!={'source','finalized','anchor'} or observed['source']!='PUBLIC_RPC_NONCE_OBSERVED'
                    or observed['finalized'] is not True):raise ValueError('nonce evidence')
            at=datetime.fromisoformat(state['history'][statuses.index('NONCE_OBSERVED')]['at'])
            _validate_anchor(observed['anchor'],terms['creation_nonce'],at)
            if (int(observed['anchor']['owner_nonce'])<=int(terms['nonce'])
                    or int(observed['anchor']['block_number'])<int(terms['anchor']['block_number'])):raise ValueError('nonce unconsumed')
            if state['receipt'] is not None:require_receipt_before_observation(state['receipt'],observed['anchor'])
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('El registro de financiación BNB no es coherente') from None
    return row,terms,state


def _public(row,terms,state,now):
    return {'id':row.id,'account_id':row.account_id,'binding_id':row.binding_id,'draft_id':terms['draft_id'],
        'status':row.status,'plan_digest':row.payload_digest,'amount_wei':terms['amount_wei'],'nonce':terms['nonce'],
        'estimated_gas_wei':str(int(terms['gas'])*int(terms['gas_price'])),'max_gas_wei':terms['max_gas_wei'],
        'expires_at':terms['expires_at'],'expired':_time(now)>=datetime.fromisoformat(terms['expires_at']),
        'created_at':row.created_at.isoformat(),'receipt':state['receipt'],'nonce_observation':state['nonce_observation'],
        'automatic_execution':False}


def _compile(terms,wallet,recipient):
    return {'from':wallet,'to':recipient,'value':hex(int(terms['amount_wei'])),'data':'0x','chainId':'0x38',
        'type':'0x0','nonce':hex(int(terms['nonce'])),'gas':hex(int(terms['gas'])),'gasPrice':hex(int(terms['gas_price']))}


def _live(db,*,binding_id,account_id,wallet,drafts,draft_id,snapshot,now):
    if type(drafts) is not DexOperatorDrafts or type(snapshot) is not FundingState:
        raise DexExecutorError('Revisa la preparación y el saldo de la operadora')
    recipient=drafts.require_verified(identifier=draft_id,account_id=account_id,wallet_address=wallet,now=now)
    from app.portfolio.dex_gas_return import require_available
    require_available(db,recipient)
    parent,binding,_=bindings.binding_record(db,binding_id)
    require_settled_policy_controls(db,parent.deployment_id)
    grant=operator.grant_view(db,parent.grant_id,now=now)
    _,terms,_=operator.grant_record(db,parent.grant_id)
    state=snapshot.executor
    _context(db,parent.deployment_id,account_id,wallet,state,now)
    if (parent.account_id!=account_id or parent.status!='READY' or grant['status']!='ACTIVE' or grant['expired']
            or grant['unresolved_outcomes'] or state.finalized is not True
            or state.operator!=recipient or state.current_operator_code_empty is not True
            or state.policy_limits is None or state.policy_limits.as_dict()!=terms['limits']
            or str(state.policy_id)!=terms['policy_id'] or _identity(db,'wallet',recipient)!=terms['operator_digest']
            or type(snapshot.operator_balance) is not int or not 0<=snapshot.operator_balance<2**256):
        raise DexExecutorError('El vínculo, la operadora, la política o el presupuesto cambió; revisa de nuevo')
    return parent,terms,grant,recipient


def prepare_funding(db:Session,*,identifier:str,account_id:str,binding_id:str,draft_id:str,wallet_address:str,
                    drafts:DexOperatorDrafts,amount_bnb:str,gas:str,gas_price:str,max_gas_bnb:str,snapshot:FundingState,now:datetime)->dict:
    now=_time(now);identifier,account_id,binding_id,draft_id=[str(UUID(v)) for v in (identifier,account_id,binding_id,draft_id)]
    wallet=canonical_wallet_address(wallet_address)
    amount=exact_units(amount_bnb,18);maximum=exact_units(max_gas_bnb,18)
    gas_n=_uint(gas,'Gas',maximum=100000);price=_uint(gas_price,'Precio gas',maximum=10**12)
    if gas_n<21000 or gas_n*price>maximum or maximum>MAX_GAS_BUDGET or amount>MAX_GAS_BUDGET:
        raise DexExecutorError('Importe BNB o límite de gas no válido')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');_schema(db)
    parent,grant,budget,recipient=_live(db,binding_id=binding_id,account_id=account_id,wallet=wallet,drafts=drafts,
        draft_id=draft_id,snapshot=snapshot,now=now)
    prior=db.get(RealDexGasFundingIntent,identifier)
    if prior is not None:
        row,terms,state=funding_record(db,identifier)
        expected={'account_id':account_id,'binding_id':binding_id,'draft_id':draft_id,'wallet_digest':_identity(db,'wallet',wallet),
            'amount_wei':str(amount),'gas':gas,'gas_price':gas_price,'max_gas_wei':str(maximum)}
        if any(terms[k]!=v for k,v in expected.items()):raise DexExecutorError('El identificador de envío BNB ya tiene otros datos')
        return {**_public(row,terms,state,now),'transaction':_compile(terms,wallet,recipient)}
    if amount+snapshot.operator_balance>int(budget['remaining_gas_wei']):raise DexExecutorError('El envío supera el gas restante por financiar')
    if amount+maximum>snapshot.executor.native_balance:raise DexExecutorError('BNB insuficiente para el importe y el gas máximo')
    terms={'schema':'orbis.dex.gas.funding.v1','id':identifier,'account_id':account_id,'binding_id':binding_id,'draft_id':draft_id,
        'deployment_id':parent.deployment_id,'wallet_digest':_identity(db,'wallet',wallet),'operator_digest':grant['operator_digest'],
        'policy_id':grant['policy_id'],'creation_nonce':deployment_record(db,parent.deployment_id)[1]['nonce'],
        'amount_wei':str(amount),'operator_balance':str(snapshot.operator_balance),'owner_balance':str(snapshot.executor.native_balance),
        'remaining_gas_wei':budget['remaining_gas_wei'],'nonce':str(snapshot.executor.pending_nonce),
        'gas':gas,'gas_price':gas_price,'max_gas_wei':str(maximum),'anchor':_anchor(db,snapshot.executor),
        'expires_at':_expiry(now,grant['limits']).isoformat()}
    state={'status':'PREPARED','history':[{'status':'PREPARED','at':now.isoformat()}],'tx_ref':None,'receipt':None,'nonce_observation':None}
    raw,digest=_encoded(terms);state_raw,state_digest=_encoded(state)
    row=RealDexGasFundingIntent(id=identifier,account_id=account_id,binding_id=binding_id,wallet_digest=terms['wallet_digest'],
        payload_json=raw,payload_digest=digest,state_json=state_raw,state_digest=state_digest,status='PREPARED',created_at=now,updated_at=now)
    db.add(row);db.flush();funding_record(db,identifier)
    result={**_public(row,terms,state,now),'transaction':_compile(terms,wallet,recipient)}
    db.commit();return result


def _advance(db,row,state,status,now,*,commit=True):
    if now<row.updated_at:raise DexExecutorError('El reloj del envío BNB retrocedió')
    state['status']=row.status=status;row.updated_at=now;state['history'].append({'status':status,'at':now.isoformat()})
    row.state_json,row.state_digest=_encoded(state);db.flush()
    if commit:funding_record(db,row.id);db.commit()


def claim_funding(db:Session,identifier:str,*,account_id:str,wallet_address:str,drafts:DexOperatorDrafts,
                  plan_digest:str,snapshot:FundingState,now:datetime)->dict:
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    now=_time(now);wallet=canonical_wallet_address(wallet_address)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=funding_record(db,identifier)
    if (row.account_id!=account_id or _identity(db,'wallet',wallet)!=row.wallet_digest or row.payload_digest!=plan_digest):
        raise DexExecutorError('Wallet, cuenta o revisión del envío BNB distinta')
    if row.status!='PREPARED':return {**_public(row,terms,state,now),'claim_granted':False}
    _,grant,budget,recipient=_live(db,binding_id=row.binding_id,account_id=account_id,wallet=wallet,drafts=drafts,
        draft_id=terms['draft_id'],snapshot=snapshot,now=now)
    current=_anchor(db,snapshot.executor)
    if (now>=datetime.fromisoformat(terms['expires_at']) or current['pending_nonce']!=terms['nonce']
            or current['counters']!=terms['anchor']['counters'] or current['operator_ref']!=terms['anchor']['operator_ref']
            or int(terms['amount_wei'])+snapshot.operator_balance>int(budget['remaining_gas_wei'])
            or int(terms['amount_wei'])+int(terms['max_gas_wei'])>snapshot.executor.native_balance):
        raise DexExecutorError('El estado o el saldo BNB cambió; revisa de nuevo')
    assert_wallet_nonce_available(db,wallet,terms['nonce'])
    _advance(db,row,state,'WALLET_REQUESTED',now)
    return {**_public(row,terms,state,now),'claim_granted':True}


def report_funding(db:Session,identifier:str,*,account_id:str,now:datetime,tx_hash:str|None=None,rejected:bool=False)->dict:
    now=_time(now)
    if (type(rejected) is not bool or rejected and tx_hash is not None
            or not rejected and (not isinstance(tx_hash,str) or re.fullmatch('0x[0-9a-fA-F]{64}',tx_hash) is None)):
        raise DexExecutorError('Indica el hash original o el rechazo de la wallet')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=funding_record(db,identifier)
    if row.account_id!=account_id:raise DexExecutorError('Cuenta de envío BNB distinta')
    from app.portfolio.dex_native_resolution import require_unresolved
    require_unresolved(db,'GAS_FUNDING',row.id)
    reference=_identity(db,'tx',tx_hash) if tx_hash else None
    target='WALLET_REJECTED' if rejected else 'HASH_REPORTED'
    if (row.status==target or not rejected and row.status in {'FINALIZED','REVERTED','NONCE_OBSERVED'}) and state['tx_ref']==reference:
        return _public(row,terms,state,now)
    if row.status!='WALLET_REQUESTED' and not (row.status=='NONCE_OBSERVED' and state['tx_ref'] is None and not rejected):
        raise DexExecutorError('La respuesta BNB ya se registró o no se solicitó')
    _unique_hash(db,row.id,reference)
    state['tx_ref']=reference;_advance(db,row,state,target,now)
    return _public(row,terms,state,now)


def _unique_hash(db,identifier,reference):
    if reference is not None:
        for item in db.scalars(select(RealDexGasFundingIntent).where(RealDexGasFundingIntent.id!=identifier)):
            if funding_record(db,item.id)[2]['tx_ref']==reference:raise DexExecutorError('La transacción ya corresponde a otro envío BNB')


def _observe(db,row,terms,state,evidence,now):
    try:
        raw=json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(raw)>100000 or set(evidence)!={'chain_id','tx_hash','transaction','receipt','block','finalized'}:raise ValueError('shape')
        tx_hash=word(evidence['tx_hash']);reference=_identity(db,'tx',tx_hash)
        if evidence['chain_id']!='0x38' or state['tx_ref'] not in (None,reference):raise ValueError('hash')
        tx,receipt,block,final=[evidence[k] for k in ('transaction','receipt','block','finalized')]
        recipient=canonical_wallet_address(tx['to'])
        owner,kind,gas,cap,priority=recover_wallet_transfer(tx,expected_to=recipient,expected_value=int(terms['amount_wei']))
        if (_identity(db,'wallet',owner)!=row.wallet_digest or _identity(db,'wallet',recipient)!=terms['operator_digest']
                or quantity(tx['nonce'])!=int(terms['nonce']) or not 21000<=gas<=100000 or not 0<cap<=10**12
                or gas*cap>int(terms['max_gas_wei'])):raise ValueError('signed transfer')
        height,block_hash,at=finalized_block(block,final,created_at=row.created_at,now=now)
        if (height<int(terms['anchor']['block_number']) or word(tx['hash'])!=tx_hash or word(receipt['transactionHash'])!=tx_hash
                or quantity(receipt['type'])!=kind or canonical_wallet_address(receipt['from'])!=owner
                or canonical_wallet_address(receipt['to'])!=recipient or receipt['contractAddress'] is not None or receipt['logs']!=[]
                or quantity(receipt['transactionIndex'])!=quantity(tx['transactionIndex'])
                or any(quantity(item['blockNumber'])!=height or word(item['blockHash'])!=block_hash for item in (tx,receipt))):
            raise ValueError('receipt linkage')
        used,price,success=[quantity(receipt[k]) for k in ('gasUsed','effectiveGasPrice','status')]
        base=quantity(block['baseFeePerGas']) if kind==2 else None
        if (not 21000<=used<=gas or success not in (0,1) or kind==2 and cap<base
                or price!=(min(cap,base+priority) if kind==2 else cap)):raise ValueError('gas')
        result={'status':'FINALIZED' if success else 'REVERTED','source':'WALLET_RPC_OBSERVED',
            'block_number':str(height),'block_hash':block_hash,'block_at':at.isoformat(),'transaction_type':kind,
            'gas_limit':str(gas),'gas_used':str(used),'fee_cap':str(cap),'priority_fee':str(priority) if priority is not None else None,
            'base_fee':str(base) if base is not None else None,'effective_gas_price':str(price),'gas_fee_wei':str(used*price),
            'transferred_wei':terms['amount_wei'] if success else '0','observation_ref':_identity(db,'receipt',raw)}
        observed=state['nonce_observation'];require_receipt_before_observation(result,observed['anchor'] if observed else None)
        return result,reference
    except DexExecutorError:raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,BadSignature,ValidationError):
        raise DexExecutorError('El recibo BNB no coincide con el envío revisado; no se repetirá') from None


def check_funding(db:Session,identifier:str,*,account_id:str,evidence:dict,now:datetime,commit:bool=True)->dict:
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=funding_record(db,identifier)
    if row.account_id!=account_id or row.status not in {'WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED','FINALIZED','REVERTED'}:
        raise DexExecutorError('Revisa la solicitud original del envío BNB')
    from app.portfolio.dex_native_resolution import require_unresolved
    require_unresolved(db,'GAS_FUNDING',row.id)
    receipt,reference=_observe(db,row,terms,state,evidence,now)
    if row.status in {'FINALIZED','REVERTED'}:
        if {k:v for k,v in receipt.items() if k!='observation_ref'}!={k:v for k,v in state['receipt'].items() if k!='observation_ref'}:
            raise DexExecutorError('El recibo BNB ya registrado es distinto')
        return _public(row,terms,state,now)
    _unique_hash(db,row.id,reference)
    if state['tx_ref'] is None:
        state['tx_ref']=reference;_advance(db,row,state,'HASH_REPORTED',now,commit=False)
    state['receipt']=receipt;_advance(db,row,state,receipt['status'],now,commit=commit)
    return _public(row,terms,state,now)


def recover_funding(db:Session,identifier:str,*,account_id:str,wallet_address:str,snapshot:FundingState,now:datetime)->dict:
    now=_time(now);wallet=canonical_wallet_address(wallet_address)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=funding_record(db,identifier)
    if row.account_id!=account_id or _identity(db,'wallet',wallet)!=row.wallet_digest:raise DexExecutorError('Wallet o cuenta BNB distinta')
    from app.portfolio.dex_native_resolution import require_unresolved
    require_unresolved(db,'GAS_FUNDING',row.id)
    if row.status not in {'WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED'}:raise DexExecutorError('Revisa el envío BNB original')
    if state['nonce_observation'] is not None:return _public(row,terms,state,now)
    if type(snapshot) is not FundingState:raise DexExecutorError('Revisa el nonce BNB en un bloque finalizado')
    current=snapshot.executor;_context(db,terms['deployment_id'],account_id,wallet,current,now)
    if not current.finalized or current.owner_nonce<=int(terms['nonce']) or current.block_number<int(terms['anchor']['block_number']):
        raise DexExecutorError('El nonce BNB original aún no está consumido en un bloque finalizado')
    state['nonce_observation']={'source':'PUBLIC_RPC_NONCE_OBSERVED','finalized':True,'anchor':_anchor(db,current)}
    _advance(db,row,state,'NONCE_OBSERVED',now)
    return _public(row,terms,state,now)


def list_funding(db:Session,account_id:str,*,now:datetime)->list[dict]:
    _schema(db)
    from app.portfolio.dex_native_resolution import status
    result=[]
    for identifier in db.scalars(select(RealDexGasFundingIntent.id).where(RealDexGasFundingIntent.account_id==str(UUID(account_id))).order_by(RealDexGasFundingIntent.created_at.desc()).limit(100)):
        item=_public(*funding_record(db,identifier),now);resolved=status(db,'GAS_FUNDING',identifier)
        result.append({**item,**({'resolution':resolved} if resolved else {})})
    return result


def verify_all_funding(db:Session,*,version:int)->None:
    if version<63:
        names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'real_dex_gas_funding_intents' in names and db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_gas_funding_intents').scalar_one():
            raise DexExecutorError('Una bóveda anterior no admite financiación BNB de operadoras')
        return
    hashes=set();nonces=set();unknown=set()
    for item in db.scalars(select(RealDexGasFundingIntent)):
        row,terms,state=funding_record(db,item.id)
        if state['tx_ref'] is not None:
            if state['tx_ref'] in hashes:raise DexExecutorError('Recibo BNB duplicado')
            hashes.add(state['tx_ref'])
        if row.status not in {'PREPARED','WALLET_REJECTED'}:
            key=(row.wallet_digest,terms['nonce'])
            if key in nonces:raise DexExecutorError('Nonce BNB duplicado')
            nonces.add(key)
        if row.status=='WALLET_REQUESTED':
            from app.portfolio.dex_native_resolution import status
            if status(db,'GAS_FUNDING',row.id) is not None:continue
            if row.wallet_digest in unknown:raise DexExecutorError('Solicitudes BNB sin resolver duplicadas')
            unknown.add(row.wallet_digest)
