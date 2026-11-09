"""Authenticated source port for supplemental order proofs; no writes or RPC."""
from dataclasses import replace
from uuid import UUID
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_receipt_order import OrderSource,Reference,validate_order
from app.portfolio.dex_deployments import _identity
from app.portfolio.wallet_funding import _encoded
from app.portfolio.wallet_proof import canonical_wallet_address


def _load(db: Session,kind: str,identifier: str,*,wallet: str,archived_proof: dict|None=None) -> OrderSource:
    from app.portfolio.dex_call_resolution import source_record as original,_reference,resolution_record
    from app.portfolio.dex_call_booking import _wallet_source,_financial
    from app.portfolio.dex_receipt_booking import source_record as economic
    identifier=str(UUID(identifier))
    if kind not in ('WALLET_SWAP','OPERATOR','CALL_RESOLUTION'):raise DexExecutorError('Origen DEX sin operación económica compatible')
    if kind=='CALL_RESOLUTION':
        resolution=resolution_record(db,identifier)
        if resolution is None:raise DexExecutorError('Resolución DEX no encontrada')
        _,payload=resolution
        parent,terms,state,call=original(db,payload['source_kind'],payload['source_id'],owner=wallet)
        source=economic(db,kind,identifier);tx_ref=payload['proof']['tx_ref']
    else:
        parent,terms,state,call=original(db,kind,identifier,owner=wallet)
        source=(_wallet_source(db,parent,terms,state,wallet) if kind=='WALLET_SWAP' else economic(db,kind,identifier))
        if kind=='WALLET_SWAP' and archived_proof is not None:
            # Only the authenticated certificate can attest the mapping from
            # wallet-v1's private HMAC domain to the common owner domain.
            source=replace(source,wallet_digest=archived_proof['owner_digest'],signer_digest=archived_proof['signer_ref'])
        tx_ref=state['tx_digest' if kind=='WALLET_SWAP' else 'tx_ref']
    if not isinstance(source.receipt,dict) or not _financial(source):
        raise DexExecutorError('El origen no demuestra una ejecución propia finalizada')
    if archived_proof is None and (source.wallet_digest!=_identity(db,'wallet',wallet)
            or call.kind=='WALLET_SWAP' and call.signer_digest!=_reference(db,call.kind)('wallet',wallet)):
        raise DexExecutorError('El origen pertenece a otra wallet')
    return OrderSource(kind,source.id,source.account_id,source.plan_digest,source.receipt_digest,source.wallet_digest,source.signer_digest,call,
        _encoded(source.receipt)[0],tx_ref,source.updated_at,source.transaction_index)


def order_source(db: Session,kind: str,identifier: str,*,wallet_address: str) -> OrderSource:
    return _load(db,kind,identifier,wallet=canonical_wallet_address(wallet_address))


def archived_order_source(db: Session,kind: str,identifier: str,*,proof: dict) -> OrderSource:
    """Reopen from authenticated originals and a sealed mapping, never an address.

    The dummy is used solely to construct the old pure call description. The
    archive validator does not regenerate calldata or make network requests.
    No unvalidated mapping leaves this function.
    """
    from app.portfolio.dex_call_resolution import DUMMY
    try:
        source=_load(db,kind,identifier,wallet=DUMMY,archived_proof=proof)
        validate_order(source,proof,reference=order_reference(db,source))
        return source
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('La prueba de orden perdió su origen autenticado') from None


def order_reference(db: Session,source: OrderSource) -> Reference:
    """Original tx/signer domains and a separate supplemental proof domain."""
    from app.portfolio.dex_call_resolution import _reference
    original=_reference(db,source.call.kind)
    return lambda label,value:(_identity(db,'tx',value) if label=='dex-order-tx'
        else _identity(db,'wallet',value) if label in ('dex-order-signer','dex-order-owner')
        else original(label,value) if label in ('tx','wallet') else _identity(db,label,value))

