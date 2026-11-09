"""Preparación revisable OCO; sin firma, reserva ni permiso de envío.

La envoltura autenticada vive en el cliente, ligada a su sesión local. Todos
los uint64/tiempos ns son cadenas para no perder precisión al pasar por JS.
"""

from decimal import Decimal
import hashlib
import hmac
import secrets
from uuid import uuid4

from sqlalchemy import select

from app.live_execution.grvt_testnet_oco import (
    GrvtTestnetOcoError, _encode, _parent, instrument_terms, oco_client_ids, require_no_pending_oco,
)
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits
from app.live_execution.grvt_testnet_protection import GrvtTestnetTrigger, compile_testnet_protection
from app.live_execution.grvt_testnet_quote import fetch_testnet_quote
from app.live_execution.grvt_testnet_reconciliation import require_reconciled_continuity
from app.live_execution.grvt_testnet_risk import fetch_testnet_risk
from app.live_execution.grvt_testnet_signer import fetch_testnet_instrument
from app.portfolio.models import GrvtTestnetOcoGroup, GrvtTestnetOrderIntent, GrvtTestnetStop


MAX_PROTECTION_NOTIONAL = Decimal('1000')  # Sólo reducción TESTNET; IOC no cambia.
NO_BUILDER = '0x'+'0'*40
PREVIEW_LIFETIME_NS = 120_000_000_000


def protection_source(vault, sub: str, parent_id: str, *, group_id: str | None = None) -> tuple[dict,str]:
    with vault.session() as db:
        stop = db.get(GrvtTestnetStop,sub)
        row = db.get(GrvtTestnetOrderIntent,parent_id)
        if (stop is not None and stop.stopped or row is None or row.sub_account_id != sub
                or group_id is not None and db.get(GrvtTestnetOcoGroup,group_id) is not None):
            raise GrvtTestnetOcoError('Origen detenido, ajeno o grupo ya reservado')
        source,digest = _parent(db,parent_id,latest=True)
        require_no_pending_oco(db,sub)
        if db.scalar(select(GrvtTestnetOrderIntent.client_order_id).where(
                GrvtTestnetOrderIntent.sub_account_id == sub,
                GrvtTestnetOrderIntent.status.in_(('PREPARED','POST_CLAIMED','ACKNOWLEDGED','OBSERVED'))).limit(1)):
            raise GrvtTestnetOcoError('Otra orden testnet sigue pendiente')
        require_reconciled_continuity(db,sub,{'settle_currency':'USDT',**source['result']})
        return source,digest


def preview_protection(vault, client, *, sub: str, parent_id: str, cap: Decimal,
                       stop_loss: Decimal, take_profit: Decimal, slippage_bps: int,
                       ttl_minutes: int, public_post, clock_ns) -> dict:
    if not 0 < cap <= MAX_PROTECTION_NOTIONAL:
        raise GrvtTestnetOcoError('Tope de salidas TESTNET: 1000 USDT')
    source,digest = protection_source(vault,sub,parent_id)
    instrument_name = source['result']['positions'][0][0]
    instrument = fetch_testnet_instrument(instrument_name,http_post=public_post)
    state,rules = fetch_testnet_risk(client,instrument_name,sub,public_post=public_post,clock_ns=clock_ns)
    with vault.session() as db:
        require_reconciled_continuity(db,sub,{'settle_currency':state.settle_currency,
            'settle_balance':format(state.settle_balance,'f'),
            'positions':[[name,format(size,'f')] for name,size in state.positions]},now_ns=clock_ns())
    quote = fetch_testnet_quote(instrument_name,http_post=public_post,clock_ns=clock_ns)
    now = clock_ns()
    limits = GrvtTestnetLimits(sub,instrument_name,cap)
    group_id = str(uuid4())
    ids = oco_client_ids(group_id)
    nonces = [secrets.randbits(32),secrets.randbits(32)]
    while nonces[0] == nonces[1]: nonces[1] = secrets.randbits(32)
    expiration = now+ttl_minutes*60_000_000_000
    legs = []
    for role,price,cid,nonce in zip(('STOP_LOSS','TAKE_PROFIT'),(stop_loss,take_profit),ids,nonces):
        draft = compile_testnet_protection(state,rules,instrument,limits,quote,
            trigger=GrvtTestnetTrigger(role,price),max_slippage_bps=slippage_bps,
            builder=NO_BUILDER,client_order_id=cid,nonce=nonce,expiration_ns=expiration,now_ns=now)
        legs.append({'role':role,'client_order_id':cid,'nonce':nonce,
            'trigger_price':format(price,'f'),'limit_price':format(draft.limit_price,'f')})
    return {'schema':'orbis.grvt.testnet.oco.preview.v1','network':'TESTNET','group_id':group_id,
        'sub_account_id':sub,'parent_client_order_id':parent_id,'parent_receipt_digest':digest,
        'instrument':instrument_name,'instrument_terms':instrument_terms(instrument),
        'side':draft.side,'size':format(draft.size,'f'),'max_order_notional':format(cap,'f'),
        'last_price':format(quote.last_price,'f'),'max_slippage_bps':slippage_bps,
        'created_at_ns':str(now),'expires_at_ms':min(now+PREVIEW_LIFETIME_NS,expiration)//1_000_000,
        'signature_expiration_ns':str(expiration),'legs':legs}


def preview_mac(preview: dict, key: bytes, token: str) -> str:
    encoded = _encode(preview)[0].encode('ascii')
    if len(encoded) > 16_384: raise GrvtTestnetOcoError('Preparación OCO demasiado grande')
    return hmac.new(key,b'orbis-grvt-testnet-oco-preview-v1\0'+token.encode('ascii')+b'\0'+encoded,
                    hashlib.sha256).hexdigest()


def verify_preview(preview: dict, mac: str, key: bytes, token: str, *, now_ns: int) -> None:
    try:
        if (not secrets.compare_digest(preview_mac(preview,key,token),mac)
                or preview['schema'] != 'orbis.grvt.testnet.oco.preview.v1'
                or preview['network'] != 'TESTNET'
                or not 0 <= now_ns-int(preview['created_at_ns']) <= PREVIEW_LIFETIME_NS
                or now_ns//1_000_000 >= preview['expires_at_ms']):
            raise ValueError('invalid preview')
    except Exception:
        raise GrvtTestnetOcoError('Preparación OCO alterada, caducada o de otra sesión') from None


def sign_reviewed_protection(vault, preview: dict, *, private_key: str, expected_signer: str, now_ns: int):
    """Reconstruye exactamente el plan autenticado; no acepta órdenes ajenas."""
    import re
    from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument,GrvtTestnetLimitDraft
    from app.live_execution.grvt_testnet_protection import sign_testnet_protection
    _,digest = protection_source(vault,preview['sub_account_id'],preview['parent_client_order_id'],
                                group_id=preview['group_id'])
    if digest != preview['parent_receipt_digest']:
        raise GrvtTestnetOcoError('El origen OCO cambió desde la preparación')
    if not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{64}',private_key):
        raise GrvtTestnetOcoError('Clave dedicada TESTNET inválida')
    terms = preview['instrument_terms']
    instrument = GrvtTestnetInstrument(preview['instrument'],int(terms['asset_id']),terms['base_decimals'],
        Decimal(terms['tick_size']),Decimal(terms['min_size']),Decimal(terms['min_notional']))
    limits = GrvtTestnetLimits(preview['sub_account_id'],preview['instrument'],Decimal(preview['max_order_notional']))
    if limits.max_order_notional > MAX_PROTECTION_NOTIONAL:
        raise GrvtTestnetOcoError('Tope OCO TESTNET excedido')
    orders = []
    for leg in preview['legs']:
        draft = GrvtTestnetLimitDraft(limits.sub_account_id,limits.instrument,preview['side'],
            Decimal(preview['size']),Decimal(leg['limit_price']),True,NO_BUILDER,
            leg['client_order_id'],leg['nonce'],int(preview['signature_expiration_ns']))
        orders.append(sign_testnet_protection(draft,limits,instrument,
            GrvtTestnetTrigger(leg['role'],Decimal(leg['trigger_price'])),
            dedicated_private_key=bytes.fromhex(private_key.removeprefix('0x')),
            expected_signer=expected_signer,now_ns=now_ns))
    return orders,limits,instrument
