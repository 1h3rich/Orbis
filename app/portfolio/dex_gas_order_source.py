"""Authenticated gas-only source snapshots. No writes, RPC or authority.

Existing record validators retain their own rules and receipt versions. The
new certificate adds order without adapting a transfer/creation/cancellation
into a zero-value trade, or inferring any principal from a gas expense.
"""
from __future__ import annotations

import hmac
from uuid import UUID
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError,creation_transaction
from app.live_execution.dex_gas_order import GasCall,GasSource,validate_gas_order
from app.portfolio import dex_receipt_booking as bookings,dex_call_booking as calls
from app.portfolio.dex_deployments import _identity
from app.portfolio.dex_verified_read import scope
from app.portfolio.economic_projection import GAS_SOURCE_KINDS
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.wallet_funding import _encoded
from app.portfolio.wallet_proof import canonical_wallet_address


def _original(db,kind,identifier):
    if kind=='WALLET_SWAP':
        from app.portfolio.wallet_swap import swap_record
        return swap_record(db,identifier)
    return bookings._registry()[kind][1](db,identifier)


def _fingerprint(row):
    return {'id':row.id,'account_id':row.account_id,'definition':row.payload_digest,
        'state':getattr(row,'state_digest',None),'status':row.status,
        'created_at':row.created_at.isoformat(),'updated_at':row.updated_at.isoformat()}


def _creation(terms,wallet):
    tx=creation_transaction(wallet,terms['fee'],nonce=int(terms['nonce']),gas=int(terms['gas']),gas_price=int(terms['gas_price']))
    return GasCall('CREATE',wallet,None,tx['data'])


def _call_spec(call,wallet):
    tx=call.expected_call();return GasCall('CALL',wallet,tx['to'],tx['data'])


def _load(db:Session,kind:str,identifier:str,*,wallet:str|None,proof:dict|None)->GasSource:
    if kind not in GAS_SOURCE_KINDS:raise DexExecutorError('Fuente de gas DEX no compatible')
    identifier=str(UUID(identifier));_existing_salt(db)
    from app.portfolio.dex_call_resolution import DUMMY,source_record as call_original
    live=wallet is not None;address=wallet or DUMMY
    parent_kind=kind;parent_id=identifier;resolution=None;known_index=None;spec=None;extra=None
    if kind=='CALL_RESOLUTION':
        from app.portfolio.dex_call_resolution import resolution_record
        resolution=resolution_record(db,identifier)
        if resolution is None:raise DexExecutorError('Resolución de llamada no encontrada')
        row,payload=resolution;parent_kind=payload['source_kind'];parent_id=payload['source_id']
        parent,terms,state,call=call_original(db,parent_kind,parent_id,owner=address)
        financial=calls.source_record(db,identifier);outcome=payload['proof']['outcome'];observation=payload['proof']
        original_signer=call.signer_digest;transaction_ref=observation['tx_ref'];known_index=int(observation['transaction_index'])
        extra=_encoded(call.grant)[1]if call.grant is not None else None
        if outcome=='CANCELLED':signature='CANCEL';spec=GasCall('CANCEL',address)if live else None
        elif outcome=='REVERTED':
            signature='FAILED';spec=GasCall('FAILED',address,call_digest=observation['call_digest'])if live else None
        else:signature='CALL';spec=_call_spec(call,address)if live else None
    elif kind=='DEPLOYMENT_RESOLUTION':
        from app.portfolio.dex_deployment_resolution import resolution_record
        resolution=resolution_record(db,identifier)
        if resolution is None:raise DexExecutorError('Resolución de despliegue no encontrada')
        row,payload=resolution;parent_kind='DEPLOYMENT'
        parent,terms,state=_original(db,parent_kind,parent_id);financial=bookings.source_record(db,kind,identifier)
        observation=payload['proof'];outcome=observation['outcome'];transaction_ref=observation['tx_ref'];original_signer=terms['wallet_digest']
        known_index=int(observation['transaction_index']);signature='CANCEL'if outcome=='CANCELLED'else 'CREATE'
        if live:spec=GasCall('CANCEL',address)if signature=='CANCEL'else _creation(terms,address)
    elif kind=='NATIVE_RESOLUTION':
        from app.portfolio.dex_native_resolution import resolution_record
        resolution=resolution_record(db,identifier)
        if resolution is None:raise DexExecutorError('Resolución nativa no encontrada')
        row,payload=resolution;parent_kind=payload['source_kind'];parent_id=payload['source_id']
        parent,terms,state=_original(db,parent_kind,parent_id);financial=bookings.source_record(db,kind,identifier)
        observation=payload['proof'];outcome=observation['outcome'];transaction_ref=observation['tx_ref'];original_signer=row.signer_digest
        known_index=int(observation['transaction_index']);signature='CANCEL'if outcome=='CANCELLED'else 'NATIVE'
        target_ref=terms['operator_digest']if parent_kind=='GAS_FUNDING'else terms['wallet_digest']
        if live:spec=GasCall('CANCEL',address)if signature=='CANCEL'else GasCall('NATIVE',address,value=int(observation['signed_amount_wei']),target_ref=target_ref)
    else:
        parent,terms,state=_original(db,kind,identifier);row=parent
        if parent.status not in ('FINALIZED','REVERTED')or not isinstance(state.get('receipt'),dict):
            raise DexExecutorError('Comprueba primero el recibo finalizado de gas')
        if kind in ('WALLET_SWAP','OPERATOR'):
            _,_,_,call=call_original(db,kind,identifier,owner=address)
            financial=calls._wallet_source(db,parent,terms,state,address)if kind=='WALLET_SWAP'else bookings.source_record(db,kind,identifier)
            original_signer=call.signer_digest;extra=_encoded(call.grant)[1]if call.grant is not None else None
            signature='CALL';spec=_call_spec(call,address)if live else None
        else:
            financial=bookings.source_record(db,kind,identifier)
            original_signer=terms['operator_digest']if kind=='GAS_RETURN'else terms['wallet_digest']
            signature={'DEPLOYMENT':'CREATE','CONTROL':'CONTROL','GAS_FUNDING':'NATIVE','GAS_RETURN':'NATIVE'}[kind]
            if live:
                if signature=='CREATE':spec=_creation(terms,address)
                elif signature=='CONTROL':spec=GasCall('CONTROL',address,control_terms_json=_encoded(terms)[0])
                else:
                    target_ref=terms['operator_digest']if kind=='GAS_FUNDING'else terms['wallet_digest']
                    spec=GasCall('NATIVE',address,value=int(terms['amount_wei']),target_ref=target_ref)
        transaction_ref=state['tx_digest'if kind=='WALLET_SWAP'else 'tx_ref']
        outcome='REVERTED'if financial.receipt['status']=='REVERTED'else 'DEPLOYED'if kind=='DEPLOYMENT'else 'TRANSFERRED'if signature=='NATIVE'else 'EXECUTED'
    if (financial.receipt is None or financial.receipt['status']not in ('FINALIZED','REVERTED')
            or calls._financial(financial)):
        raise DexExecutorError('El origen necesita un recibo finalizado sin compra o venta ejecutada')
    # A legacy wallet's private owner domain is mapped only with the live
    # address, or with the already sealed certificate during archive reopening.
    if parent_kind=='WALLET_SWAP':
        if live:
            from app.portfolio.wallet_swap import _identity as old_identity
            if not hmac.compare_digest(terms['wallet_digest'],old_identity(db,'wallet',address)):
                raise DexExecutorError('El gas pertenece a otra wallet')
            owner=_identity(db,'wallet',address);signer=owner
        else:
            if proof is None:raise DexExecutorError('Falta el vínculo sellado de la wallet original')
            owner,signer=proof['owner_digest'],proof['signer_ref']
            if owner!=signer:raise DexExecutorError('Firmante de wallet distinto de su propietario')
    else:
        owner=terms['owner_digest']if parent_kind=='OPERATOR'else terms['wallet_digest']
        signer=original_signer
        if live and not hmac.compare_digest(owner,_identity(db,'wallet',address)):
            raise DexExecutorError('El gas pertenece a otra wallet')
    receipt=financial.receipt
    snapshot={'source':_fingerprint(row),'parent':_fingerprint(parent),'grant_digest':extra,
        'receipt_digest':financial.receipt_digest,'parent_kind':parent_kind,'parent_id':parent_id}
    return GasSource(kind,row.id,row.account_id,row.payload_digest,financial.receipt_digest,_encoded(snapshot)[1],owner,signer,
        original_signer,parent_kind,parent_id,financial.action,outcome,signature,int(terms['nonce']),
        int(receipt.get('gas_limit',terms.get('gas',21000))),int(receipt.get('fee_cap',terms.get('gas_price',0))),
        _encoded(receipt)[0],transaction_ref,parent.created_at,row.updated_at,known_index,spec)


def gas_source(db:Session,kind:str,identifier:str,*,wallet_address:str)->GasSource:
    try:
        with scope(db,force_full=True):
            return _load(db,kind,identifier,wallet=canonical_wallet_address(wallet_address),proof=None)
    except DexExecutorError:raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('El origen del gas cambió o no conserva su recibo finalizado')from None


def gas_reference(db:Session,source:GasSource):
    if source.parent_kind=='WALLET_SWAP':from app.portfolio.wallet_swap import _identity as original
    elif source.parent_kind=='OPERATOR':from app.portfolio.dex_operator_journal import _identity as original
    elif source.parent_kind=='CONTROL':from app.portfolio.dex_controls import _identity as original
    elif source.parent_kind=='GAS_FUNDING':from app.portfolio.dex_gas_funding import _identity as original
    elif source.parent_kind=='GAS_RETURN':from app.portfolio.dex_gas_return import _identity as original
    else:original=_identity
    from app.portfolio.dex_controls import _identity as control_identity
    def reference(label,value):
        if label=='tx':return _identity(db,'tx',value)if source.kind in ('NATIVE_RESOLUTION','DEPLOYMENT_RESOLUTION')else original(db,'tx',value)
        if label in ('wallet','call-resolution-data'):return original(db,label,value)
        if label=='control-operator':return control_identity(db,'operator',value)
        if label=='gas-order-wallet':return _identity(db,'wallet',value)
        if label=='gas-order-tx':return _identity(db,'tx',value)
        return _identity(db,label,value)
    return reference


def archived_gas_source(db:Session,kind:str,identifier:str,*,proof:dict)->GasSource:
    try:
        with scope(db,force_full=True):
            source=_load(db,kind,identifier,wallet=None,proof=proof)
            validate_gas_order(source,proof,reference=gas_reference(db,source));return source
    except DexExecutorError:raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('La prueba de gas perdió su origen autenticado')from None
