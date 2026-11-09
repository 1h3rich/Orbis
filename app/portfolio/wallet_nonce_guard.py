"""Exclusión de solicitudes EOA entre ingreso MEXC y DEX, dentro del write lock.

No posee un lock propio ni abre otra sesión/transacción: la concesión actual
ya mantiene BEGIN IMMEDIATE. Un hash conocido no libera su nonce; un resultado
desconocido bloquea cualquier nonce de esa wallet, también en el otro módulo.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.domain import PortfolioError
from app.portfolio.models import RealWalletFundingIntent, RealWalletSwapIntent, RealDexDeploymentIntent, RealDexControlIntent, RealDexGasFundingIntent


def assert_wallet_nonce_available(db: Session, wallet: str, nonce: str) -> None:
    _scan_wallet_nonce(db,wallet,nonce)


def assert_funding_cancellation_available(db:Session,wallet:str,nonce:str,funding_id:str)->None:
    """The only exception is one verified native funding at its exact nonce."""
    from app.portfolio import dex_gas_funding as funding,dex_native_resolution as native
    row,terms,_=funding.funding_record(db,funding_id)
    if (row.wallet_digest!=funding._identity(db,'wallet',wallet) or terms['nonce']!=nonce
            or row.status not in {'WALLET_REQUESTED','HASH_REPORTED'} or native.status(db,'GAS_FUNDING',row.id) is not None):
        raise PortfolioError('La financiación original ya cambió o se resolvió')
    _scan_wallet_nonce(db,wallet,nonce,cancel_funding_id=row.id)


def _scan_wallet_nonce(db:Session,wallet:str,nonce:str,*,cancel_funding_id:str|None=None)->None:
    # Imports diferidos: ambos módulos usan la misma guarda al conceder.
    from app.portfolio.wallet_funding import _identity as funding_identity, funding_record
    from app.portfolio.wallet_swap import _identity as swap_identity, swap_record
    from app.portfolio.dex_deployments import _identity as deployment_identity, deployment_record
    from app.portfolio.dex_controls import _identity as control_identity, control_record
    from app.portfolio.dex_operator_journal import assert_operator_nonce_available
    from app.portfolio.dex_gas_funding import _identity as gas_identity

    assert_operator_nonce_available(db, wallet, nonce)

    journals=[(RealWalletFundingIntent, funding_identity, funding_record),
                                     (RealWalletSwapIntent, swap_identity, swap_record),
                                     (RealDexDeploymentIntent, deployment_identity, deployment_record),
                                     (RealDexControlIntent, control_identity, control_record)]
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=63:
        # Distinguish the native funding journal from the MEXC ERC-20 journal.
        from app.portfolio.dex_gas_funding import funding_record as gas_record
        journals.append((RealDexGasFundingIntent,gas_identity,gas_record))
    else:
        from app.portfolio.dex_gas_funding import verify_all_funding
        verify_all_funding(db,version=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one())
    for model, identity, record in journals:
        rows = db.scalars(select(model).where(model.wallet_digest == identity(db, "wallet", wallet))).all()
        for item in rows:
            row, terms, _state = record(db, item.id)
            if model is RealWalletSwapIntent and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78:
                from app.portfolio.dex_external_outcomes import external_outcome
                if external_outcome(db,'WALLET_SWAP',row.id)is not None:
                    if int(nonce)<=int(terms['nonce']):raise PortfolioError('El nonce de la llamada externa ya está consumido')
                    continue
            if model is RealWalletSwapIntent and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
                from app.portfolio.dex_call_resolution import status as resolution_status
                if resolution_status(db,'WALLET_SWAP',row.id) is not None:
                    if terms['nonce']==nonce:raise PortfolioError('Este nonce ya corresponde a un swap resuelto')
                    continue
            if model is RealDexGasFundingIntent and row.id==cancel_funding_id:continue
            if model is RealDexDeploymentIntent:
                from app.portfolio.dex_deployment_resolution import resolution_record
                if resolution_record(db,row.id) is not None:
                    if terms['nonce']==nonce:raise PortfolioError('Este nonce ya corresponde a una solicitud resuelta')
                    continue
            if row.status == "WALLET_REQUESTED":
                if model is RealDexGasFundingIntent:
                    from app.portfolio.dex_native_resolution import status
                    if status(db,'GAS_FUNDING',row.id) is not None:
                        if terms['nonce']==nonce:raise PortfolioError('Este nonce ya corresponde a un envío BNB resuelto')
                        continue
                raise PortfolioError("Hay una solicitud de esta wallet pendiente sin resultado; revísala antes de otro envío")
            if model is RealWalletFundingIntent and row.status == "CHAIN_CLOSED" and int(nonce) <= int(terms["nonce"]):
                raise PortfolioError("El nonce de este ingreso ya está consumido")
            if row.status in {"HASH_REPORTED", "FINALIZED", "REVERTED", "CODE_OBSERVED", "NONCE_OBSERVED"} and terms["nonce"] == nonce:
                raise PortfolioError("Este nonce ya se utilizó en otra acción de la wallet; revisión necesaria")
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=70:
        from app.portfolio.dex_wallet_cancellation import require_wallet_nonce_available
        require_wallet_nonce_available(db,wallet,nonce,exclude_funding_id=cancel_funding_id)
