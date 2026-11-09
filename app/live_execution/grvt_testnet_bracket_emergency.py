"""Reducción TESTNET con STOP: ámbito acotado, diario IOC y conciliación comunes.

Una concesión no desactiva STOP ni autoriza compras, transferencias o mainnet.
Cada ID pasa por PREPARED → POST_CLAIMED antes de red. Tras incertidumbre sólo
se consulta; un nuevo intento exige conciliar el anterior y otro ID firmado.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal,localcontext
import json
import time
from typing import Callable,TYPE_CHECKING

import httpx
from sqlalchemy import select

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import _verified
from app.live_execution.grvt_testnet_continuity import _anchor
from app.live_execution.grvt_testnet_oco import _encode,_stamp,instrument_terms
from app.live_execution.grvt_testnet_oco_preflight import _safe
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits,GrvtTestnetOrderClient,_validate_order
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote,GrvtTestnetPriceGuard,fetch_testnet_quote,guard_testnet_limit_price
from app.live_execution.grvt_testnet_reconciliation import _number,_positions,_ns
from app.live_execution.grvt_testnet_signer import (
    GrvtTestnetInstrument,fetch_testnet_instrument,verify_testnet_limit_signature,_exact_uint,_UINT64_MAX,
)
from app.live_execution.grvt_testnet_risk import _public_post
from app.portfolio.models import (
    GrvtTestnetBracketGroup,GrvtTestnetBracketEmergency,GrvtTestnetBracketReconciliation,
    GrvtTestnetOrderIntent,GrvtTestnetReconciliation,GrvtTestnetStop,
)
from app.portfolio.vault import RealVault

if TYPE_CHECKING:
    from app.live_execution.grvt_testnet_journal import GrvtTestnetRecord


def has_emergency_table(db) -> bool:
    present=db.connection().exec_driver_sql(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='grvt_testnet_bracket_emergencies'"
    ).scalar_one()==1
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=41 and not present:
        raise GrvtTestnetBracketError('Esquema de emergencia OTOCO incompleto')
    return present


def _instrument(plan):
    terms=plan['instrument_terms']
    return GrvtTestnetInstrument(plan['instrument'],int(terms['asset_id']),terms['base_decimals'],
        _number(terms['tick_size']),_number(terms['min_size']),_number(terms['min_notional']))


def _terms(order):
    leg=order['legs'][0]
    return {'instrument':leg['instrument'],'side':'BUY' if leg['is_buying_asset'] else 'SELL',
        'size':leg['size'],'limit_price':leg['limit_price'],'reduce_only':order['reduce_only'],
        'time_in_force':order['time_in_force'],'builder':order['builder'],'builder_fee':order['builder_fee'],
        'nonce':order['signature']['nonce'],'expiration_ns':int(order['signature']['expiration'])}


def _source(db,parent,*,now_ns,client_order_id,historical):
    group=db.get(GrvtTestnetBracketGroup,parent)
    if group is None: raise GrvtTestnetBracketError('Grupo de origen no encontrado')
    plan=_verified(db,group)
    anchor=_anchor(db,group.sub_account_id,now_ns=now_ns,client_order_id=client_order_id,historical=historical)
    if anchor is None: raise GrvtTestnetBracketError('Falta un recibo conciliado de origen')
    if anchor.bracket_parent_id==parent:
        receipt=db.get(GrvtTestnetBracketReconciliation,parent)
    elif anchor.bracket_parent_id is None and anchor.oco_group_id is None:
        prior=db.get(GrvtTestnetBracketEmergency,anchor.client_order_id)
        if prior is None or prior.parent_client_order_id!=parent:
            raise GrvtTestnetBracketError('La última operación pertenece a otro origen')
        # El recibo verificado por _anchor ya valida su preflight v3 y ámbito.
        receipt=db.get(GrvtTestnetReconciliation,anchor.client_order_id)
    else: raise GrvtTestnetBracketError('El último recibo pertenece a otro origen')
    result=json.loads(receipt.payload_json)['result']
    positions=_positions(result['positions'])
    if set(positions)!={plan['instrument']}:
        raise GrvtTestnetBracketError('No queda una posición única conciliada que reducir')
    return group,plan,anchor,result,positions[plan['instrument']]


def _quote(raw):
    if (not isinstance(raw,dict) or set(raw)!=set(GrvtTestnetQuote.__dataclass_fields__)
            or any(type(raw[key]) is not int for key in ('event_time_ns','observed_at_ns'))):
        raise ValueError('quote fields')
    return GrvtTestnetQuote(**{**raw,**{key:_number(raw[key]) for key in
        ('bid','ask','bid_size','ask_size')},'last_price':_number(raw['last_price']) if raw['last_price'] is not None else None})


def verified_emergency_scope(db,intent,*,now_ns=None,order=None,historical=False) -> dict:
    """El ámbito se recalcula al reclamar, enviar, leer o verificar una copia."""
    try:
        if intent is None or not has_emergency_table(db): raise ValueError('missing emergency intent')
        row=db.get(GrvtTestnetBracketEmergency,intent.client_order_id)
        if row is None: raise ValueError('not an emergency')
        p=json.loads(row.payload_json)
        fields={'schema','parent_client_order_id','client_order_id','sub_account_id','plan_digest',
            'source_client_order_id','source_receipt_digest','issued_at_ns','stop_at_ns','wire_digest','contract','quote'}
        if (set(p)!=fields or _encode(p)!=(row.payload_json,row.payload_digest)
                or p['schema']!='orbis.grvt.testnet.bracket.emergency.v1'
                or p['parent_client_order_id']!=row.parent_client_order_id
                or p['client_order_id']!=intent.client_order_id or p['sub_account_id']!=intent.sub_account_id
                or type(p['issued_at_ns']) is not int or type(p['stop_at_ns']) is not int
                or _stamp(p['issued_at_ns'])!=intent.created_at or p['wire_digest']!=intent.request_digest):
            raise ValueError('invalid emergency scope')
        at=p['issued_at_ns'] if historical else now_ns
        if type(at) is not int or not 0<=at-p['issued_at_ns']<=5_000_000_000:
            raise ValueError('stale emergency')
        group,plan,anchor,_,held=_source(db,row.parent_client_order_id,now_ns=at,
            client_order_id=intent.client_order_id,historical=historical)
        stop=db.get(GrvtTestnetStop,intent.sub_account_id)
        from app.live_execution.grvt_stop_control import stop_was_active
        if (intent.sub_account_id!=group.sub_account_id or p['plan_digest']!=group.payload_digest
                or p['source_client_order_id']!=anchor.client_order_id
                or p['source_receipt_digest']!=anchor.receipt_digest or stop is None
                or not (stop_was_active(db,intent.sub_account_id,p['stop_at_ns']) if historical else stop.stopped)
                or not 0<p['stop_at_ns']<=p['issued_at_ns'] or _ns(stop.updated_at)<p['stop_at_ns']):
            raise ValueError('source or STOP changed')
        contract=p['contract']
        fields={'instrument','side','size','limit_price','reduce_only','time_in_force','builder','builder_fee','nonce','expiration_ns'}
        if (not isinstance(contract,dict) or set(contract)!=fields or contract['instrument']!=plan['instrument']
                or contract['side']!=('SELL' if held>0 else 'BUY') or contract['reduce_only'] is not True
                or contract['time_in_force']!='IMMEDIATE_OR_CANCEL' or contract['builder']!='0x'+'0'*40
                or contract['builder_fee']!='0' or type(contract['nonce']) is not int
                or not 0<=contract['nonce']<2**32 or type(contract['expiration_ns']) is not int
                or not at<contract['expiration_ns']<=p['issued_at_ns']+10_000_000_000):
            raise ValueError('not a bounded IOC reduction')
        size,price=_number(contract['size']),_number(contract['limit_price'])
        instrument=_instrument(plan)
        _exact_uint(size,instrument.base_decimals,_UINT64_MAX); _exact_uint(price,9,_UINT64_MAX)
        with localcontext() as ctx:
            ctx.prec=100
            if (not instrument.min_size<=size<=abs(held) or price<=0 or price%instrument.tick_size
                    or not instrument.min_notional<=size*price<=_number(plan['exit_cap'])
                    or (intent.instrument,intent.side,intent.size,intent.limit_price,intent.max_order_notional)!=
                        (plan['instrument'],contract['side'],size,price,_number(plan['exit_cap']))):
                raise ValueError('quantity or cap exceeded')
        quote=_quote(p['quote'])
        if quote.instrument!=intent.instrument: raise ValueError('quote scope')
        guard_testnet_limit_price(intent.side,price,quote,**plan['price_policy'],now_ns=at)
        if order is not None:
            from app.live_execution.grvt_testnet_journal import _digest
            limits=GrvtTestnetLimits(intent.sub_account_id,intent.instrument,intent.max_order_notional)
            if (_validate_order(order,limits,at)!=intent.client_order_id or _terms(order)!=contract
                    or _digest(order)!=intent.request_digest): raise ValueError('other signed request')
            verify_testnet_limit_signature(order,limits,instrument,now_ns=at)
        return p
    except Exception:
        raise GrvtTestnetBracketError('Ámbito de emergencia OTOCO no verificable') from None


def read_emergency_attempts(db,parent) -> list[dict]:
    if not has_emergency_table(db): return []
    result=[]
    for row in db.scalars(select(GrvtTestnetBracketEmergency).where(
            GrvtTestnetBracketEmergency.parent_client_order_id==parent)):
        intent=db.get(GrvtTestnetOrderIntent,row.client_order_id)
        p=verified_emergency_scope(db,intent,historical=True)
        result.append({'client_order_id':row.client_order_id,'status':intent.status,
            'issued_at_ns':p['issued_at_ns'],'size':p['contract']['size'],'side':intent.side})
    return sorted(result,key=lambda row:(row['issued_at_ns'],row['client_order_id']))


def verify_all_emergencies(db) -> None:
    if not has_emergency_table(db): return
    for row in db.scalars(select(GrvtTestnetBracketEmergency)):
        verified_emergency_scope(db,db.get(GrvtTestnetOrderIntent,row.client_order_id),historical=True)


def require_no_unreconciled_order(db,sub_account_id: str) -> None:
    if db.scalar(select(GrvtTestnetOrderIntent.client_order_id).where(
        GrvtTestnetOrderIntent.sub_account_id==sub_account_id,
        GrvtTestnetOrderIntent.status.in_(('PREPARED','POST_CLAIMED','ACKNOWLEDGED','OBSERVED'))).limit(1)) is not None:
        raise GrvtTestnetBracketError('Otra orden sigue sin conciliar')


def _reserve(vault: RealVault,parent: str,order: dict,quote: GrvtTestnetQuote,*,now_ns: int) -> None:
    from app.live_execution.grvt_testnet_journal import _digest,_require_reconciled
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            cid=order['metadata']['client_order_id']
            if db.get(GrvtTestnetOrderIntent,cid) is not None:
                raise GrvtTestnetBracketError('Emergencia ya archivada; consulta su ID, no reenvíes')
            group,plan,anchor,_,_=_source(db,parent,now_ns=now_ns,client_order_id=cid,historical=False)
            _require_reconciled(db,group.sub_account_id,cid)
            require_no_unreconciled_order(db,group.sub_account_id)
            stop=db.get(GrvtTestnetStop,group.sub_account_id)
            if stop is None or not stop.stopped: raise GrvtTestnetBracketError('Activa STOP antes de cerrar por emergencia')
            terms=_terms(order); stamp=_stamp(now_ns)
            intent=GrvtTestnetOrderIntent(client_order_id=cid,sub_account_id=group.sub_account_id,
                instrument=terms['instrument'],side=terms['side'],size=_number(terms['size']),
                limit_price=_number(terms['limit_price']),max_order_notional=_number(plan['exit_cap']),
                request_digest=_digest(order),status='PREPARED',created_at=stamp,updated_at=stamp)
            db.add(intent); db.flush()
            p={'schema':'orbis.grvt.testnet.bracket.emergency.v1','parent_client_order_id':parent,
                'client_order_id':cid,'sub_account_id':group.sub_account_id,'plan_digest':group.payload_digest,
                'source_client_order_id':anchor.client_order_id,'source_receipt_digest':anchor.receipt_digest,
                'issued_at_ns':now_ns,'stop_at_ns':_ns(stop.updated_at),'wire_digest':intent.request_digest,
                'contract':terms,'quote':_safe(asdict(quote))}
            encoded,digest=_encode(p)
            db.add(GrvtTestnetBracketEmergency(client_order_id=cid,parent_client_order_id=parent,
                payload_json=encoded,payload_digest=digest)); db.flush()
            verified_emergency_scope(db,intent,now_ns=now_ns,order=order)
            db.commit()
        except Exception:
            db.rollback(); raise


def submit_bracket_emergency_once(vault: RealVault,client: GrvtTestnetOrderClient,parent: str,order: dict,*,
        public_post: Callable[[str,dict],httpx.Response]=_public_post,
        clock_ns: Callable[[],int]=time.time_ns) -> GrvtTestnetRecord:
    """Una orden firmada acotada. Fallo o timeout no autoriza repetir su POST."""
    from app.live_execution.grvt_testnet_submit import submit_testnet_once
    last=clock_ns(); _stamp(last)
    def checked():
        nonlocal last
        at=clock_ns(); _stamp(at)
        if at<last: raise GrvtTestnetBracketError('Reloj de emergencia regresivo')
        last=at; return at
    # Copia privada: el llamador no cambia el contrato durante las lecturas.
    order=json.loads(_encode(order)[0]); cid=order['metadata']['client_order_id']
    with vault.session() as db:
        if db.get(GrvtTestnetOrderIntent,cid) is not None:
            raise GrvtTestnetBracketError('Emergencia ya archivada; consulta su ID, no reenvíes')
        group,plan,_,_,held=_source(db,parent,now_ns=checked(),client_order_id=cid,historical=False)
        stop=db.get(GrvtTestnetStop,group.sub_account_id)
        if stop is None or not stop.stopped: raise GrvtTestnetBracketError('Activa STOP antes de cerrar por emergencia')
        limits=GrvtTestnetLimits(group.sub_account_id,plan['instrument'],_number(plan['exit_cap']))
        instrument=_instrument(plan)
        _validate_order(order,limits,checked())
        if (order['reduce_only'] is not True or order['builder']!='0x'+'0'*40 or order['builder_fee']!='0'
                or order['legs'][0]['is_buying_asset'] is not (held<0)
                or not 0<_number(order['legs'][0]['size'])<=abs(held)):
            raise GrvtTestnetBracketError('La emergencia sólo puede reducir la posición conciliada')
        verify_testnet_limit_signature(order,limits,instrument,now_ns=checked())
    actual=fetch_testnet_instrument(instrument.name,http_post=public_post)
    if instrument_terms(actual)!=instrument_terms(instrument):
        raise GrvtTestnetBracketError('Términos de instrumento cambiados')
    quote=fetch_testnet_quote(instrument.name,http_post=public_post,clock_ns=checked)
    _reserve(vault,parent,order,quote,now_ns=checked())
    def before_claim() -> GrvtTestnetPriceGuard:
        from app.live_execution.grvt_testnet_bracket_finance import read_bracket_audits
        from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
        from app.live_execution.grvt_testnet_oco_finance import canonical_observed_order
        original=read_bracket_audits(vault,parent)[-1]
        observed=observe_bracket_orders(vault,client,parent,clock_ns=checked)
        keys=('client_order_id','order_id','status','size','traded_size','update_time_ns')
        for actual,leg in zip(observed['orders'],original['legs']):
            if canonical_observed_order({key:actual[key] for key in keys})!=canonical_observed_order(
                    {key:leg['order'][key] for key in keys}):
                raise GrvtTestnetBracketError('Una orden original cambió después de conciliar; revisa el grupo')
        latest=fetch_testnet_quote(instrument.name,http_post=public_post,clock_ns=checked)
        return GrvtTestnetPriceGuard(latest,**plan['price_policy'])
    return submit_testnet_once(vault,client,order,limits,public_post=public_post,clock_ns=checked,
        pre_claim_guard=before_claim)
