"""Authenticated review catalogue, including optional native fee-only receipts.

No RPC, writes or order authority. Resolved originals have one effective source;
executed trades retain their financial profile rather than becoming gas rows.
"""
from sqlalchemy import select
from app.portfolio import models as m, dex_receipt_booking as books, dex_call_booking as calls
from app.portfolio.dex_deployments import _identity
from app.portfolio.dex_gas_order_source import gas_source
from app.portfolio.wallet_proof import canonical_wallet_address


def review_sources(db, wallet_address: str, *, include_gas: bool = False):
    wallet = canonical_wallet_address(wallet_address)
    owner = _identity(db, 'wallet', wallet)
    for source in calls._sources(db, wallet, owner):
        yield source, False
    if not include_gas:
        return
    from app.portfolio import wallet_swap, dex_deployment_resolution, dex_native_resolution, dex_call_resolution
    for kind, (model, _) in books._registry(db).items():
        for row in db.scalars(select(model).where(model.status.in_(('FINALIZED', 'REVERTED')))):
            source = books.source_record(db, kind, row.id)
            if source.wallet_digest != owner or calls._financial(source):
                continue
            if kind == 'OPERATOR' and dex_call_resolution.status(db, kind, row.id) is not None:
                continue
            if kind == 'DEPLOYMENT' and dex_deployment_resolution.resolution_record(db, row.id) is not None:
                continue
            if kind in ('GAS_FUNDING', 'GAS_RETURN') and dex_native_resolution.status(db, kind, row.id) is not None:
                continue
            gas_source(db, kind, row.id, wallet_address=wallet)
            yield source, True
    original_owner = wallet_swap._identity(db, 'wallet', wallet)
    for row in db.scalars(select(m.RealWalletSwapIntent).where(m.RealWalletSwapIntent.status.in_(('FINALIZED', 'REVERTED')))):
        parent, terms, state = wallet_swap.swap_record(db, row.id)
        if parent.wallet_digest != original_owner or dex_call_resolution.status(db, 'WALLET_SWAP', row.id) is not None:
            continue
        source = calls._wallet_source(db, parent, terms, state, wallet)
        if calls._financial(source):
            continue
        gas_source(db, 'WALLET_SWAP', row.id, wallet_address=wallet)
        yield source, True
