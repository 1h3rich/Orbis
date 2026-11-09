"""Reservas duraderas OTOCO TESTNET. Preparación no concede envío."""

import json
from decimal import Decimal,localcontext
import re

from sqlalchemy import select

from app.live_execution.grvt_testnet_bracket import (
    GrvtTestnetBracketError,bracket_client_ids,canonical_bracket_order,verify_testnet_bracket,validate_testnet_bracket,BRACKET_FIELDS,
)
from app.live_execution.grvt_testnet_oco import _encode,_stamp,_number,instrument_terms
from app.live_execution.grvt_testnet_preflight import _signal_source
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument,_exact_uint,_UINT64_MAX
from app.live_execution.grvt_testnet_protection import GrvtTestnetTrigger,PROTECTION_LIFETIME_NS
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits,GrvtTestnetOrderAck
from app.portfolio.models import (
    GrvtTestnetBracketGroup,GrvtTestnetBracketLeg,GrvtTestnetBracketClaim,GrvtTestnetBracketObservation,GrvtTestnetBracketCancel,
    GrvtTestnetOrderIntent,GrvtTestnetOcoLeg,GrvtTestnetStop,
)


ROLES = ('ENTRY','STOP_LOSS','TAKE_PROFIT')


def _has_table(db) -> bool:
    from app.portfolio.vault import SCHEMA_VERSION
    connection = db.connection()
    version = connection.exec_driver_sql('PRAGMA user_version').scalar_one()
    names = set(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('grvt_testnet_bracket_groups','grvt_testnet_bracket_legs')").scalars())
    expected = {'grvt_testnet_bracket_groups','grvt_testnet_bracket_legs'}
    if (not 1 <= version <= SCHEMA_VERSION or version >= 37 and names != expected
            or names and names != expected):
        raise GrvtTestnetBracketError('Esquema de reservas OTOCO incompleto o desconocido')
    if version>=38 and (not _has_claim_table(db) or not _has_observation_table(db)):
        raise GrvtTestnetBracketError('Esquema de concesiones OTOCO incompleto')
    if version>=39 and not _has_cancel_table(db):
        raise GrvtTestnetBracketError('Esquema de cancelación OTOCO incompleto')
    if version>=40 and not _has_audit_tables(db):
        raise GrvtTestnetBracketError('Esquema financiero OTOCO incompleto')
    if version>=41:
        from app.live_execution.grvt_testnet_bracket_emergency import has_emergency_table
        has_emergency_table(db)
    if version>=42 and connection.exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='grvt_testnet_strategy_closes'"
            ).scalar_one()!=1:
        raise GrvtTestnetBracketError('Esquema de cierre de estrategia incompleto')
    return names == expected


def _has_claim_table(db):
    return db.connection().exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='grvt_testnet_bracket_claims'").scalar_one()==1


def _has_observation_table(db):
    return db.connection().exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='grvt_testnet_bracket_observations'").scalar_one()==1


def _has_cancel_table(db):
    return db.connection().exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='grvt_testnet_bracket_cancels'").scalar_one()==1


def _has_audit_tables(db):
    return db.connection().exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('grvt_testnet_bracket_audits','grvt_testnet_bracket_audit_heads','grvt_testnet_bracket_reconciliations')").scalar_one()==3


def _verified(db,row) -> dict:
    try:
        if not _has_table(db): raise ValueError('missing reservation schema')
        p = json.loads(row.payload_json)
        if (_encode(p) != (row.payload_json,row.payload_digest)
                or set(p) != BRACKET_FIELDS
                or p['schema'] != 'orbis.grvt.testnet.bracket.v1'
                or p['parent_client_order_id'] != row.parent_client_order_id
                or p['sub_account_id'] != row.sub_account_id
                or row.status not in ('PREPARED','POST_CLAIMED','ACKNOWLEDGED','ABANDONED')
                or _stamp(p['created_at_ns']) != row.created_at or row.updated_at < row.created_at):
            raise ValueError('invalid group')
        source = _signal_source(p['signal_source'],row.parent_client_order_id)
        if source['action'] != 'OPEN': raise ValueError('entry source required')
        ids = bracket_client_ids(row.parent_client_order_id)
        if not isinstance(p['legs'],list) or len(p['legs']) != 3: raise ValueError('three reserved legs required')
        terms = p['instrument_terms']
        if set(terms) != {'asset_id','base_decimals','tick_size','min_size','min_notional'}:
            raise ValueError('instrument fields')
        instrument = GrvtTestnetInstrument(p['instrument'],int(terms['asset_id']),terms['base_decimals'],
            _number(terms['tick_size']),_number(terms['min_size']),_number(terms['min_notional']))
        if _encode(terms) != _encode(instrument_terms(instrument)): raise ValueError('noncanonical instrument rules')
        entry_cap,exit_cap = _number(p['entry_cap']),_number(p['exit_cap'])
        GrvtTestnetLimits(row.sub_account_id,p['instrument'],entry_cap)
        policy = p['price_policy']
        if (not isinstance(policy,dict) or set(policy) != {'max_spread_bps','max_slippage_bps'}
                or any(type(value) is not int or not 0 <= value <= 100 for value in policy.values())):
            raise ValueError('price policy')
        size = _number(p['legs'][0]['size'])
        side = 'BUY' if source['direction'] == 'LONG' else 'SELL'
        with localcontext() as ctx:
            ctx.prec = 100
            _exact_uint(size,instrument.base_decimals,_UINT64_MAX)
            for index,leg in enumerate(p['legs']):
                if (set(leg) != {'role','client_order_id','side','size','limit_price','trigger_price','nonce','expiration_ns','wire_digest'}
                        or leg['role'] != ROLES[index] or leg['client_order_id'] != ids[index]
                        or leg['side'] != (side if index == 0 else 'SELL' if side == 'BUY' else 'BUY')
                        or _number(leg['size']) != size or size < instrument.min_size
                        or type(leg['nonce']) is not int or not 0 <= leg['nonce'] < 2**32
                        or type(leg['expiration_ns']) is not int
                        or not p['created_at_ns'] < leg['expiration_ns'] <= p['created_at_ns']+(300_000_000_000 if index == 0 else PROTECTION_LIFETIME_NS)
                        or not isinstance(leg['wire_digest'],str) or not re.fullmatch(r'[a-f0-9]{64}',leg['wire_digest'])):
                    raise ValueError('reserved leg fields')
                price = _number(leg['limit_price']); _exact_uint(price,9,_UINT64_MAX)
                if price % instrument.tick_size or size*price < instrument.min_notional or size*price > (entry_cap if index == 0 else exit_cap):
                    raise ValueError('leg minimum, tick or cap')
                if index == 0:
                    if leg['trigger_price'] is not None: raise ValueError('parent trigger forbidden')
                else:
                    trigger = GrvtTestnetTrigger(leg['role'],_number(leg['trigger_price']))
                    deviation = (price-trigger.price)*10000/trigger.price
                    if (trigger.price % instrument.tick_size or size*trigger.price > exit_cap
                            or not (0 <= deviation <= policy['max_slippage_bps'] if leg['side'] == 'BUY'
                                    else -policy['max_slippage_bps'] <= deviation <= 0)):
                        raise ValueError('exit bound')
        entry,stop,target = p['legs']
        if (len({leg['nonce'] for leg in p['legs']}) != 3
                or stop['expiration_ns'] != target['expiration_ns'] or stop['expiration_ns'] <= entry['expiration_ns']
                or stop['expiration_ns'] < p['created_at_ns']+60_000_000_000
                or not (Decimal(stop['trigger_price']) < Decimal(entry['limit_price']) < Decimal(target['trigger_price'])
                    if side == 'BUY' else Decimal(target['trigger_price']) < Decimal(entry['limit_price']) < Decimal(stop['trigger_price']))):
            raise ValueError('linked shape')
        reserved = list(db.scalars(select(GrvtTestnetBracketLeg).where(
            GrvtTestnetBracketLeg.parent_client_order_id == row.parent_client_order_id)))
        if sorted((leg.client_order_id,leg.role) for leg in reserved) != sorted(zip(ids,ROLES)):
            raise ValueError('missing reserved identities')
        if any(db.get(model,cid) is not None for cid in ids for model in (GrvtTestnetOrderIntent,GrvtTestnetOcoLeg)):
            raise ValueError('identity also used by an older order')
        claim=db.get(GrvtTestnetBracketClaim,row.parent_client_order_id) if _has_claim_table(db) else None
        if (row.status in ('PREPARED','ABANDONED')) != (claim is None):
            raise ValueError('claim/status mismatch')
        if claim is not None: _verified_claim(db,row,claim,p)
        return p
    except Exception:
        raise GrvtTestnetBracketError('Reserva OTOCO no verificable') from None


def _public(db,row) -> dict:
    p=_verified(db,row)
    claim=db.get(GrvtTestnetBracketClaim,row.parent_client_order_id) if _has_claim_table(db) else None
    from app.live_execution.grvt_testnet_bracket_observation import latest_bracket_observation
    from app.live_execution.grvt_testnet_bracket_cancel import verified_bracket_cancel
    from app.live_execution.grvt_testnet_bracket_finance import verified_bracket_audits,verified_bracket_reconciliation
    from app.live_execution.grvt_testnet_bracket_emergency import read_emergency_attempts
    return {**p,'status':row.status,'can_submit':False,'protection_guaranteed':False,
        'preflight_available':claim is not None,
        'last_observation':latest_bracket_observation(db,row,p) if _has_observation_table(db) else None,
        'cancel_attempt':verified_bracket_cancel(db,row,p) if _has_cancel_table(db) else None,
        'financial_audit_count':len(verified_bracket_audits(db,row,p)) if _has_audit_tables(db) else 0,
        'reconciliation':verified_bracket_reconciliation(db,row,p) if _has_audit_tables(db) else None,
        'emergency_attempts':read_emergency_attempts(db,row.parent_client_order_id),
        'acknowledged_orders':json.loads(claim.ack_json)['orders'] if claim is not None and claim.ack_json is not None else []}


def _verified_claim(db,row,claim,p):
    try:
        if (not isinstance(claim.claimed_at_ns,str) or not re.fullmatch(r'[0-9]{1,19}',claim.claimed_at_ns)
                or not p['created_at_ns']<=int(claim.claimed_at_ns)<p['legs'][0]['expiration_ns']
                or claim.plan_digest!=row.payload_digest or row.updated_at<_stamp(int(claim.claimed_at_ns))):
            raise ValueError('invalid claim')
        if (type(claim.observation_count) is not int or claim.observation_count<0
                or (claim.observation_count==0)!=(claim.observation_head_digest is None)
                or claim.observation_head_digest is not None and not re.fullmatch(r'[a-f0-9]{64}',claim.observation_head_digest)):
            raise ValueError('invalid observation head')
        proof=json.loads(claim.preflight_json)
        if _encode(proof)!=(claim.preflight_json,claim.preflight_digest): raise ValueError('changed preflight')
        from app.live_execution.grvt_testnet_bracket_preflight import verify_bracket_preflight
        verify_bracket_preflight(db,p,proof,now_ns=int(claim.claimed_at_ns),historical=True)
        if row.status=='ACKNOWLEDGED':
            ack=json.loads(claim.ack_json)
            if (_encode(ack)!=(claim.ack_json,claim.ack_digest)
                    or set(ack)!={'parent_client_order_id','plan_digest','observed_at_ns','orders'}
                    or ack['parent_client_order_id']!=row.parent_client_order_id or ack['plan_digest']!=row.payload_digest
                    or type(ack['observed_at_ns']) is not int or ack['observed_at_ns']<int(claim.claimed_at_ns)
                    or _stamp(ack['observed_at_ns'])!=row.updated_at
                    or not isinstance(ack['orders'],list) or len(ack['orders'])!=3):
                raise ValueError('invalid ack')
            for got,expected in zip(ack['orders'],p['legs']):
                if (set(got)!={'client_order_id','order_id'} or got['client_order_id']!=expected['client_order_id']
                        or not isinstance(got['order_id'],str) or not re.fullmatch(r'[A-Za-z0-9x]{1,80}',got['order_id'])):
                    raise ValueError('different ack')
            if len({item['order_id'] for item in ack['orders']})!=3: raise ValueError('duplicate ack')
        elif claim.ack_json is not None or claim.ack_digest is not None:
            raise ValueError('ack/status mismatch')
    except Exception:
        raise GrvtTestnetBracketError('Concesión o acuse OTOCO no verificable') from None


def verify_all_brackets(db) -> None:
    if not _has_table(db): return
    for row in db.scalars(select(GrvtTestnetBracketGroup)): _public(db,row)
    for leg in db.scalars(select(GrvtTestnetBracketLeg)):
        if db.get(GrvtTestnetBracketGroup,leg.parent_client_order_id) is None:
            raise GrvtTestnetBracketError('Identidad OTOCO huérfana')
    if _has_claim_table(db):
        for claim in db.scalars(select(GrvtTestnetBracketClaim)):
            if db.get(GrvtTestnetBracketGroup,claim.parent_client_order_id) is None:
                raise GrvtTestnetBracketError('Concesión OTOCO huérfana')
    if _has_observation_table(db):
        for record in db.scalars(select(GrvtTestnetBracketObservation)):
            if db.get(GrvtTestnetBracketGroup,record.parent_client_order_id) is None:
                raise GrvtTestnetBracketError('Observación OTOCO huérfana')
    if _has_cancel_table(db):
        for record in db.scalars(select(GrvtTestnetBracketCancel)):
            if db.get(GrvtTestnetBracketGroup,record.parent_client_order_id) is None:
                raise GrvtTestnetBracketError('Cancelación OTOCO huérfana')
    if _has_audit_tables(db):
        from app.portfolio.models import GrvtTestnetBracketAudit,GrvtTestnetBracketAuditHead,GrvtTestnetBracketReconciliation
        for model in (GrvtTestnetBracketAudit,GrvtTestnetBracketAuditHead,GrvtTestnetBracketReconciliation):
            for record in db.scalars(select(model)):
                if db.get(GrvtTestnetBracketGroup,record.parent_client_order_id) is None:
                    raise GrvtTestnetBracketError('Auditoría OTOCO huérfana')
    from app.live_execution.grvt_testnet_bracket_emergency import verify_all_emergencies
    verify_all_emergencies(db)


def require_no_pending_brackets(db,sub_account_id: str) -> None:
    verify_all_brackets(db)
    if not _has_table(db): return
    for row in db.scalars(select(GrvtTestnetBracketGroup).where(GrvtTestnetBracketGroup.sub_account_id == sub_account_id)):
        if row.status != 'ABANDONED':
            from app.live_execution.grvt_testnet_bracket_finance import verified_bracket_reconciliation
            if not _has_audit_tables(db) or verified_bracket_reconciliation(db,row,_verified(db,row)) is None:
                raise GrvtTestnetBracketError('Entrada con protecciones pendiente')


def require_bracket_ids_unreserved(db,ids) -> None:
    if _has_table(db) and any(db.get(GrvtTestnetBracketLeg,cid) is not None for cid in ids):
        raise GrvtTestnetBracketError('Identidad reservada para OTOCO')


def read_bracket(vault,parent_client_order_id: str) -> dict:
    bracket_client_ids(parent_client_order_id)
    with vault.session() as db:
        row = db.get(GrvtTestnetBracketGroup,parent_client_order_id)
        if row is None: raise GrvtTestnetBracketError('Entrada con protecciones no encontrada')
        return _public(db,row)


def abandon_bracket(vault,parent_client_order_id: str,*,now_ns: int) -> dict:
    stamp = _stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = db.get(GrvtTestnetBracketGroup,parent_client_order_id)
            if row is None or row.status != 'PREPARED' or stamp < row.created_at:
                raise GrvtTestnetBracketError('Sólo puede descartarse una preparación sin envío')
            _verified(db,row)
            row.status = 'ABANDONED'; row.updated_at = stamp
            db.flush(); result = _public(db,row); db.commit(); return result
        except Exception:
            db.rollback(); raise


def prepare_bracket(vault,orders,entry_limits,exit_limits,instrument,*,signal_source: dict,now_ns: int,
                    max_spread_bps: int = 25,max_slippage_bps: int = 25) -> dict:
    try:
        orders = json.loads(_encode(orders)[0]); signal_source = json.loads(_encode(signal_source)[0])
        triggers = tuple(GrvtTestnetTrigger(order['metadata']['trigger']['trigger_type'],
            Decimal(order['metadata']['trigger']['tpsl']['trigger_price'])) for order in orders[1:])
        verify_testnet_bracket(orders,entry_limits,exit_limits,instrument,expected_triggers=triggers,now_ns=now_ns)
        parent = orders[0]['metadata']['client_order_id']
        source = _signal_source(signal_source,parent)
        if source['action'] != 'OPEN' or (source['direction'] == 'LONG') is not orders[0]['legs'][0]['is_buying_asset']:
            raise ValueError('source direction mismatch')
        legs = []
        for index,order in enumerate(orders):
            leg = order['legs'][0]
            legs.append({'role':ROLES[index],'client_order_id':order['metadata']['client_order_id'],
                'side':'BUY' if leg['is_buying_asset'] else 'SELL','size':leg['size'],'limit_price':leg['limit_price'],
                'trigger_price':None if index == 0 else order['metadata']['trigger']['tpsl']['trigger_price'],
                'nonce':order['signature']['nonce'],'expiration_ns':int(order['signature']['expiration']),
                'wire_digest':_encode(canonical_bracket_order(order))[1]})
        p = {'schema':'orbis.grvt.testnet.bracket.v1','parent_client_order_id':parent,
            'sub_account_id':entry_limits.sub_account_id,'instrument':entry_limits.instrument,
            'entry_cap':format(entry_limits.max_order_notional,'f'),'exit_cap':format(exit_limits.max_order_notional,'f'),
            'created_at_ns':now_ns,'instrument_terms':instrument_terms(instrument),'signal_source':source,
            'price_policy':{'max_spread_bps':max_spread_bps,'max_slippage_bps':max_slippage_bps},'legs':legs}
        stamp = _stamp(now_ns)
    except Exception:
        raise GrvtTestnetBracketError('No se pudo preparar el contrato OTOCO') from None
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            old = db.get(GrvtTestnetBracketGroup,parent)
            if old is not None:
                previous = _verified(db,old)
                if any(p[key] != previous[key] for key in p if key != 'created_at_ns'):
                    raise GrvtTestnetBracketError('Identidad OTOCO reutilizada con otro contrato')
                result = _public(db,old); db.commit(); return result
            from app.live_execution.grvt_testnet_oco import require_no_pending_oco
            require_no_pending_oco(db,entry_limits.sub_account_id)
            stop = db.get(GrvtTestnetStop,entry_limits.sub_account_id)
            if stop is not None and stop.stopped: raise GrvtTestnetBracketError('Subcuenta testnet detenida')
            if db.scalar(select(GrvtTestnetOrderIntent.client_order_id).where(
                    GrvtTestnetOrderIntent.sub_account_id == entry_limits.sub_account_id,
                    GrvtTestnetOrderIntent.status.in_(('PREPARED','POST_CLAIMED','ACKNOWLEDGED','OBSERVED'))).limit(1)):
                raise GrvtTestnetBracketError('Otra entrada sigue pendiente')
            require_bracket_ids_unreserved(db,[leg['client_order_id'] for leg in legs])
            if any(db.get(model,leg['client_order_id']) is not None for leg in legs
                   for model in (GrvtTestnetOrderIntent,GrvtTestnetOcoLeg)):
                raise GrvtTestnetBracketError('Identidad ya utilizada por otra orden')
            encoded,digest = _encode(p)
            row = GrvtTestnetBracketGroup(parent_client_order_id=parent,sub_account_id=entry_limits.sub_account_id,
                status='PREPARED',payload_json=encoded,payload_digest=digest,created_at=stamp,updated_at=stamp)
            db.add(row); db.flush()
            for leg in legs:
                db.add(GrvtTestnetBracketLeg(client_order_id=leg['client_order_id'],parent_client_order_id=parent,role=leg['role']))
            db.flush(); result = _public(db,row); db.commit(); return result
        except Exception:
            db.rollback(); raise


def match_bracket_orders(plan,orders,entry_limits,exit_limits,*,now_ns):
    """Compara todo el contrato firmado con la reserva, antes de consumirla."""
    if (validate_testnet_bracket(orders,entry_limits,exit_limits,now_ns=now_ns)!=bracket_client_ids(plan['parent_client_order_id'])
            or (entry_limits.sub_account_id,entry_limits.instrument)!=(plan['sub_account_id'],plan['instrument'])
            or entry_limits.max_order_notional!=_number(plan['entry_cap'])
            or exit_limits.max_order_notional!=_number(plan['exit_cap'])
            or any(_encode(canonical_bracket_order(order))[1]!=leg['wire_digest'] for order,leg in zip(orders,plan['legs']))):
        raise GrvtTestnetBracketError('Órdenes ajenas a la reserva OTOCO')


def claim_bracket(vault,parent_client_order_id,orders,entry_limits,exit_limits,*,preflight,now_ns):
    """Consume irreversiblemente un intento con revisión; no envía por sí solo."""
    stamp=_stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            if not _has_table(db) or not _has_claim_table(db):
                raise GrvtTestnetBracketError('Migración OTOCO pendiente')
            row=db.get(GrvtTestnetBracketGroup,parent_client_order_id)
            if row is None or row.status!='PREPARED':
                raise GrvtTestnetBracketError('Intento OTOCO no preparado o ya consumido')
            plan=_verified(db,row)
            stop=db.get(GrvtTestnetStop,row.sub_account_id)
            if stop is not None and stop.stopped: raise GrvtTestnetBracketError('Subcuenta testnet detenida')
            match_bracket_orders(plan,orders,entry_limits,exit_limits,now_ns=now_ns)
            from app.live_execution.grvt_testnet_bracket_preflight import verify_bracket_preflight
            text,digest=_encode(preflight)
            verify_bracket_preflight(db,plan,json.loads(text),now_ns=now_ns)
            db.add(GrvtTestnetBracketClaim(parent_client_order_id=parent_client_order_id,claimed_at_ns=str(now_ns),
                plan_digest=row.payload_digest,preflight_json=text,preflight_digest=digest))
            row.status='POST_CLAIMED'; row.updated_at=stamp
            db.flush(); result=_public(db,row); db.commit(); return result
        except Exception:
            db.rollback(); raise


def record_bracket_ack(vault,parent_client_order_id,acks,*,now_ns):
    """Acuses del grupo, no fills ni una autorización para otra entrada."""
    stamp=_stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row=db.get(GrvtTestnetBracketGroup,parent_client_order_id)
            if row is None or row.status not in ('POST_CLAIMED','ACKNOWLEDGED'):
                raise GrvtTestnetBracketError('Entrada OTOCO sin concesión de envío')
            plan=_verified(db,row)
            if (not isinstance(acks,tuple) or len(acks)!=3
                    or any(not isinstance(ack,GrvtTestnetOrderAck) or ack.sub_account_id!=row.sub_account_id
                        or ack.accepted_by_testnet is not True or ack.execution_unverified is not True for ack in acks)):
                raise GrvtTestnetBracketError('Acuses OTOCO incompletos o ajenos')
            claim=db.get(GrvtTestnetBracketClaim,parent_client_order_id)
            values=[{'client_order_id':ack.client_order_id,'order_id':ack.order_id} for ack in acks]
            if row.status=='ACKNOWLEDGED':
                if json.loads(claim.ack_json)['orders']!=values:
                    raise GrvtTestnetBracketError('Acuses OTOCO cambiados')
                return _public(db,row)
            payload={'parent_client_order_id':parent_client_order_id,'plan_digest':row.payload_digest,
                'observed_at_ns':now_ns,'orders':values}
            claim.ack_json,claim.ack_digest=_encode(payload)
            row.status='ACKNOWLEDGED'; row.updated_at=stamp
            db.flush(); result=_public(db,row); db.commit(); return result
        except Exception:
            db.rollback(); raise
