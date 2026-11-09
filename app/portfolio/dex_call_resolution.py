"""Sealed outcomes for pending wallet/operator calls; no RPC or signing."""
from datetime import datetime
import hashlib,hmac
from uuid import UUID,uuid5

from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import wallet_source,operator_source,verify_call_resolution,validate_call_proof,observation_reference
from app.live_execution.evm_transaction import word
from app.portfolio.dex_deployments import _identity,_hex_ref
from app.portfolio.models import RealDexCallResolution
from app.portfolio.wallet_funding import _encoded,_decoded,_time
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address

SCHEMA='orbis.dex.call.resolution.v1'
KINDS=('WALLET_SWAP','OPERATOR')
NAMESPACE=UUID('19fdd6e4-7565-438e-965f-382dcf59b76f')
PENDING={'WALLET_SWAP':{'WALLET_REQUESTED','HASH_REPORTED'},'OPERATOR':{'SIGNED','BROADCAST_ATTEMPTED','NONCE_OBSERVED'}}
DUMMY='0x'+'01'*20


def _version(db):return db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()


def identifier(kind,source_id):
    if kind not in KINDS:raise DexExecutorError('Fuente de llamada DEX no compatible')
    return str(uuid5(NAMESPACE,kind+':'+str(UUID(source_id))))


def source_record(db,kind,source_id,*,owner=DUMMY):
    if kind not in KINDS:raise DexExecutorError('Fuente de llamada DEX no compatible')
    if kind=='WALLET_SWAP':
        from app.portfolio.wallet_swap import swap_record
        row,terms,state=swap_record(db,str(UUID(source_id)));source=wallet_source(row,terms,state,owner)
    else:
        from app.portfolio.dex_operator_journal import intent_record,grant_record
        from app.portfolio.dex_deployments import deployment_record
        row,terms,state=intent_record(db,str(UUID(source_id)));parent,grant,_=grant_record(db,row.grant_id)
        fee=deployment_record(db,parent.deployment_id)[1]['fee'];source=operator_source(row,terms,state,grant,owner,fee)
    return row,terms,state,source


def _reference(db,kind):
    if kind=='WALLET_SWAP':from app.portfolio.wallet_swap import _identity as original
    else:from app.portfolio.dex_operator_journal import _identity as original
    return lambda label,value:original(db,label,value)


def _seal(db,body):
    return hmac.new(_salt(db),(SCHEMA+':').encode()+_encoded(body)[0].encode(),hashlib.sha256).hexdigest()


def _needs_acceptance(proof):return proof['fee_changed'] or proof['fee_overrun'] or proof['call_kind'] in {'CREATE','OTHER_CALL'}


def _quiescence(db,parent,at,*,mode=None,verify_runs=False):
    """Bound bots stop their runs; their lifetime gas grant is not replenished.

    Archive checks compare the authenticated stop witnesses without recursively
    replaying a DCA projection that can itself read these resolution records.
    The writer performs the complete run verification before attesting them.
    """
    from app.portfolio.dex_operator_journal import grant_record
    grant,_,_=grant_record(db,parent.grant_id)
    _,terms,_,_=source_record(db,'OPERATOR',parent.id)
    from app.portfolio.models import RealDexBotRun,RealDexBotDecision,RealDexBotBinding
    decision=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.payload_digest==terms['decision_ref']))
    bound=db.scalar(select(RealDexBotBinding.id).where(RealDexBotBinding.grant_id==parent.grant_id).limit(1))
    if bound is not None and terms['action']!='STOP':
        if decision is None:raise DexExecutorError('Falta la ejecución causal del bot detenido')
        event=_decoded(decision.payload_json,decision.payload_digest)
        if event['execution'] is None or any(terms.get(key)!=value for key,value in event['execution'].items()):raise DexExecutorError('La llamada no coincide con su decisión causal')
    if mode is None:mode='GRANT_STOPPED' if grant.status=='STOPPED' else 'RUNS_STOPPED'
    if mode=='GRANT_STOPPED':
        if grant.status!='STOPPED' or grant.updated_at>at:raise DexExecutorError('Detén la concesión antes de conciliar su llamada')
        return {'mode':mode,'grant_state_digest':grant.state_digest}
    if mode!='RUNS_STOPPED':raise DexExecutorError('Prueba de parada no válida')
    runs=list(db.scalars(select(RealDexBotRun).where(RealDexBotRun.grant_id==parent.grant_id,RealDexBotRun.created_at<=at).order_by(RealDexBotRun.id)))
    if not runs:raise DexExecutorError('Detén el permiso del operador antes de conciliar su llamada')
    if terms['action']!='STOP' and (decision is None or decision.run_id not in {row.id for row in runs}):raise DexExecutorError('Falta la ejecución causal del bot detenido')
    witnesses=[]
    for row in runs:
        state=_decoded(row.state_json,row.state_digest);_decoded(row.payload_json,row.payload_digest)
        if (not isinstance(state.get('history'),list) or not state['history'] or row.status!='STOPPED' or state['status']!='STOPPED'
                or state['history'][-1]!= {'status':'STOPPED','at':row.updated_at.isoformat()}
                or row.updated_at>at):raise DexExecutorError('Detén todas las ejecuciones de esta concesión antes de conciliar')
        if verify_runs:
            from app.portfolio.dex_bot_journal import run_record
            run_record(db,row.id)
        witnesses.append({'id':row.id,'payload_digest':row.payload_digest,'state_digest':row.state_digest})
    return {'mode':mode,'runs':witnesses}


def resolution_record(db,resolution_id):
    if _version(db)<71:return None
    row=db.get(RealDexCallResolution,str(UUID(resolution_id)))
    if row is None:return None
    try:
        payload=_decoded(row.payload_json,row.payload_digest)
        kind=payload['source_kind'];source_id=payload['source_id']
        parent,terms,state,source=source_record(db,kind,source_id)
        keys={'schema','id','source_kind','source_id','account_id','wallet_digest','signer_digest','nonce','tx_digest',
            'source_plan_digest','source_state_digest','source_owner_digest','source_signer_digest','quiescence','proof','accept_changes','resolved_at','seal'}
        if (set(payload)!=keys or payload['schema']!=SCHEMA or payload['id']!=row.id or row.id!=identifier(kind,source_id)
                or kind!=row.source_kind or row.swap_id!=(source_id if kind=='WALLET_SWAP' else None)
                or row.operator_id!=(source_id if kind=='OPERATOR' else None) or payload['account_id']!=row.account_id
                or parent.account_id!=row.account_id or payload['wallet_digest']!=row.wallet_digest or payload['signer_digest']!=row.signer_digest
                or payload['nonce']!=row.nonce or row.nonce!=terms['nonce'] or payload['tx_digest']!=row.tx_digest
                or any(not _hex_ref(value) for value in (row.wallet_digest,row.signer_digest,row.tx_digest))
                or payload['source_owner_digest']!=(terms['wallet_digest'] if kind=='WALLET_SWAP' else terms['owner_digest'])
                or payload['source_signer_digest']!=terms['wallet_digest']
                or payload['source_plan_digest']!=parent.payload_digest or payload['source_state_digest']!=parent.state_digest
                or parent.status not in PENDING[kind] or parent.updated_at>row.created_at or row.created_at!=row.updated_at
                or payload['resolved_at']!=row.created_at.isoformat()
                or type(payload['accept_changes']) is not bool
                or not hmac.compare_digest(payload['seal'],_seal(db,{key:value for key,value in payload.items() if key!='seal'}))):raise ValueError('source/identity/seal')
        if kind=='WALLET_SWAP':
            if row.signer_digest!=row.wallet_digest or payload['quiescence'] is not None:raise ValueError('wallet signer')
        else:
            if (row.wallet_digest!=terms['owner_digest'] or row.signer_digest!=terms['wallet_digest']
                    or payload['quiescence']!=_quiescence(db,parent,row.created_at,mode=payload['quiescence']['mode'])):raise ValueError('operator stopped/owner')
        proof=payload['proof'];validate_call_proof(proof,source,now=row.created_at)
        if not hmac.compare_digest(proof['receipt']['observation_ref'],observation_reference(source,proof,_reference(db,kind))):raise ValueError('core attestation')
        if row.status!=proof['receipt']['status'] or _needs_acceptance(proof) and not payload['accept_changes']:raise ValueError('outcome/review')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('La resolución de llamada cambió o perdió su origen verificado') from None
    return row,payload


def public_record(record):
    row,payload=record;proof=payload['proof']
    return {'id':row.id,'source_kind':row.source_kind,'source_id':payload['source_id'],'resolved':True,'outcome':proof['outcome'],
        'call_kind':proof['call_kind'],'receipt':proof['receipt'],'effect':proof['effect'],'matches_original':proof['matches_original'],
        'fee_changed':proof['fee_changed'],'fee_overrun':proof['fee_overrun'],'resolved_at':payload['resolved_at'],'automatic_execution':False}


def status(db,kind,source_id):
    record=resolution_record(db,identifier(kind,source_id));return public_record(record) if record else None


def require_unresolved(db,kind,source_id):
    if status(db,kind,source_id) is not None:raise DexExecutorError('La llamada DEX ya está resuelta; actualiza su historial')
    if _version(db)>=76:
        from app.portfolio.dex_external_observation import require_unobserved
        require_unobserved(db,kind,source_id)


def _context(db,kind,source_id,account,wallet):
    if _version(db)<71:raise DexExecutorError('Actualiza la bóveda para resolver llamadas DEX')
    owner=canonical_wallet_address(wallet);row,terms,state,source=source_record(db,kind,source_id,owner=owner)
    original_ref=_reference(db,kind)
    owner_digest=terms['wallet_digest'] if kind=='WALLET_SWAP' else terms['owner_digest']
    if row.account_id!=str(UUID(account)) or original_ref('wallet',owner)!=owner_digest:raise DexExecutorError('Llamada de otra cuenta o wallet')
    return row,terms,state,source


def preview_resolution(db,kind,source_id,*,account_id,wallet_address,evidence,accept_changes=False,now):
    now=_time(now)
    if type(accept_changes) is not bool:raise DexExecutorError('Aceptación de cambios no válida')
    row,terms,state,source=_context(db,kind,source_id,account_id,wallet_address)
    prior=resolution_record(db,identifier(kind,source_id))
    if prior:
        if prior[0].tx_digest!=_identity(db,'tx',word(evidence['tx_hash'])):raise DexExecutorError('La llamada ya se resolvió con otra transacción')
        return public_record(prior)
    if _version(db)>=76:
        from app.portfolio.dex_external_observation import require_unobserved
        require_unobserved(db,kind,source_id)
    if row.status not in PENDING[kind]:raise DexExecutorError('La llamada no necesita resolver otro recibo')
    quiescence=_quiescence(db,row,now,verify_runs=True) if kind=='OPERATOR' else None
    proof=verify_call_resolution(source,evidence,reference=_reference(db,kind),now=now)
    value={'id':identifier(kind,source_id),'source_kind':kind,'source_id':row.id,'resolved':False,'account_id':row.account_id,
        'source_plan_digest':row.payload_digest,'source_state_digest':row.state_digest,'proof':proof,'quiescence':quiescence,
        'outcome':proof['outcome'],'call_kind':proof['call_kind'],'receipt':proof['receipt'],'effect':proof['effect'],
        'fee_changed':proof['fee_changed'],'fee_overrun':proof['fee_overrun'],'accept_changes':accept_changes,
        'available':accept_changes or not _needs_acceptance(proof),'reviewed_max_gas_wei':terms['max_gas_wei'],
        'reviewed_gas_price_wei':terms['gas_price'],'automatic_execution':False}
    return {**value,'preview_digest':_encoded(value)[1]}


def require_unused(db,signer_digest,nonce,tx_digest,*,exclude=None):
    if _version(db)>=76:
        from app.portfolio.dex_external_observation import require_unused as require_external_unused
        require_external_unused(db,signer_digest,nonce,tx_digest)
    if _version(db)<71:return
    for raw in db.scalars(select(RealDexCallResolution)):
        if raw.id==exclude:continue
        row,_=resolution_record(db,raw.id)
        if row.signer_digest==signer_digest and row.nonce==nonce or row.tx_digest==tx_digest:
            raise DexExecutorError('Este nonce o recibo ya resolvió otra llamada DEX')


def _check_other_families(db,signer_digest,nonce,tx_digest):
    from app.portfolio.dex_native_resolution import resolution_record as native
    from app.portfolio.dex_deployment_resolution import resolution_record as deployment
    from app.portfolio.models import RealDexNativeResolution,RealDexDeploymentResolution
    for model,reader in ((RealDexNativeResolution,native),(RealDexDeploymentResolution,deployment)):
        for raw in db.scalars(select(model)):
            row,body=reader(db,raw.id);proof=body['proof']
            actor=row.signer_digest if model is RealDexNativeResolution else row.wallet_digest
            other_nonce=row.nonce if model is RealDexNativeResolution else proof['nonce']
            if actor==signer_digest and other_nonce==nonce or proof['tx_ref']==tx_digest:raise DexExecutorError('Este nonce o recibo ya resolvió otra operación')


def _check_originals(db,kind,source_id,sender,nonce,tx_hash):
    """Compare domains only using the verified live signer/hash, never stored raw."""
    from app.portfolio import models as m,wallet_funding as wf,wallet_swap as ws,dex_deployments as dep,dex_controls as ctl
    from app.portfolio import dex_operator_journal as op,dex_gas_funding as gas,dex_gas_return as ret
    registry=((m.RealWalletFundingIntent,wf.funding_record,wf._identity,'wallet_digest','MEXC_FUNDING'),
        (m.RealWalletSwapIntent,ws.swap_record,ws._identity,'wallet_digest','WALLET_SWAP'),
        (m.RealDexDeploymentIntent,dep.deployment_record,dep._identity,'wallet_digest','DEPLOYMENT'),
        (m.RealDexControlIntent,ctl.control_record,dep._identity,'wallet_digest','CONTROL'),
        (m.RealDexOperatorIntent,op.intent_record,op._identity,'wallet_digest','OPERATOR'),
        (m.RealDexGasFundingIntent,gas.funding_record,dep._identity,'wallet_digest','GAS_FUNDING'),
        (m.RealDexGasReturnIntent,ret.return_record,dep._identity,'operator_digest','GAS_RETURN'))
    for model,reader,ref,actor,source_kind in registry:
        for raw in db.scalars(select(model)):
            if kind==source_kind and raw.id==source_id:continue
            row,terms,state=reader(db,raw.id)
            if row.status in {'PREPARED','WALLET_REJECTED','ABANDONED_UNSENT'}:continue
            same_nonce=terms.get(actor)==ref(db,'wallet',sender) and terms['nonce']==nonce
            same_tx=state.get('tx_ref',state.get('tx_digest'))==ref(db,'tx',tx_hash)
            if same_nonce or same_tx:raise DexExecutorError('El nonce o recibo pertenece a otro original; concílialo en su origen')


def confirm_resolution(db,kind,source_id,*,account_id,wallet_address,evidence,preview_digest,source_state_digest,accept_changes=False,now):
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now)
        if type(accept_changes) is not bool:raise DexExecutorError('Aceptación de cambios no válida')
        parent,terms,state,source=_context(db,kind,source_id,account_id,wallet_address)
        prior=resolution_record(db,identifier(kind,source_id));tx_ref=_identity(db,'tx',word(evidence['tx_hash']))
        if prior:
            if prior[0].tx_digest!=tx_ref:raise DexExecutorError('El resultado ya tiene otro recibo')
            return public_record(prior)
        proposal=preview_resolution(db,kind,source_id,account_id=account_id,wallet_address=wallet_address,evidence=evidence,accept_changes=accept_changes,now=now)
        if proposal['preview_digest']!=preview_digest or parent.state_digest!=source_state_digest:raise DexExecutorError('El origen o recibo cambió; revisa de nuevo')
        if not proposal['available']:raise DexExecutorError('Acepta los cambios de tarifa o la llamada fallida distinta')
        proof=proposal['proof'];sender=canonical_wallet_address(evidence['transaction']['from'])
        signer=_identity(db,'wallet',sender);owner=_identity(db,'wallet',canonical_wallet_address(wallet_address))
        require_unused(db,signer,terms['nonce'],tx_ref);_check_other_families(db,signer,terms['nonce'],tx_ref)
        _check_originals(db,kind,parent.id,sender,terms['nonce'],word(evidence['tx_hash']))
        body={'schema':SCHEMA,'id':proposal['id'],'source_kind':kind,'source_id':parent.id,'account_id':parent.account_id,
            'wallet_digest':owner,'signer_digest':signer,'nonce':terms['nonce'],'tx_digest':tx_ref,
            'source_plan_digest':parent.payload_digest,'source_state_digest':parent.state_digest,
            'source_owner_digest':terms['wallet_digest'] if kind=='WALLET_SWAP' else terms['owner_digest'],
            'source_signer_digest':terms['wallet_digest'],'quiescence':proposal['quiescence'],
            'proof':proof,'accept_changes':accept_changes,'resolved_at':now.isoformat()}
        raw,digest=_encoded({**body,'seal':_seal(db,body)})
        db.add(RealDexCallResolution(id=body['id'],source_kind=kind,swap_id=parent.id if kind=='WALLET_SWAP' else None,
            operator_id=parent.id if kind=='OPERATOR' else None,account_id=parent.account_id,wallet_digest=owner,signer_digest=signer,
            nonce=terms['nonce'],tx_digest=tx_ref,status=proof['receipt']['status'],payload_json=raw,payload_digest=digest,created_at=now,updated_at=now))
        db.flush();result=public_record(resolution_record(db,body['id']));db.commit();return result
    except Exception:db.rollback();raise


def require_signer_nonce_available(db,wallet,nonce):
    if _version(db)>=76:
        from app.portfolio.dex_external_observation import require_actor_available
        require_actor_available(db,wallet,nonce)
    if _version(db)<71:return
    reference=_identity(db,'wallet',canonical_wallet_address(wallet))
    for raw in db.scalars(select(RealDexCallResolution).where(RealDexCallResolution.signer_digest==reference)):
        row,_=resolution_record(db,raw.id)
        if row.nonce==nonce:raise DexExecutorError('Este nonce ya tiene una llamada DEX resuelta')


def verify_all_resolutions(db,*,version):
    if version<71:
        names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'real_dex_call_resolutions' in names and db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_call_resolutions').scalar_one():
            raise DexExecutorError('Una bóveda anterior no admite resoluciones de llamadas DEX')
        return
    seen=set();hashes=set()
    for raw in db.scalars(select(RealDexCallResolution)):
        row,_=resolution_record(db,raw.id);key=(row.signer_digest,row.nonce)
        if key in seen or row.tx_digest in hashes:raise DexExecutorError('Nonce o recibo de llamada duplicado')
        seen.add(key);hashes.add(row.tx_digest);_check_other_families(db,row.signer_digest,row.nonce,row.tx_digest)
