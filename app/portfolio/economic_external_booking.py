"""Sealed multi-account manual booking, independent of the active criterion.

Private staging only. A completed transaction must also store its versioned
acceptance and a separate link; cold-file validation refuses orphan bookings.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import hmac
import json
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio import models as m, dex_external_order as orders, dex_external_observation as observations
from app.portfolio.economic_external_effects import review_arguments, validate_external_effect_units
from app.portfolio.economic_projection import ProjectionError
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.repository import _digest, _from_row
from app.portfolio.wallet_funding import _decoded, _encoded, _time

SCHEMA = 'orbis.portfolio.external.booking.v1'
WITNESS = 'orbis.portfolio.external.booked.order.witness.v1'
LINK = 'orbis.portfolio.external.acceptance.link.v1'
NAMESPACE = UUID('ed370fa4-2330-4381-b4e6-34597a7a4591')
TABLES = tuple(cls.__tablename__ for cls in (m.RealDexExternalBooking, m.RealDexExternalBookingMember,
    m.RealDexExternalBookingAccount, m.RealDexExternalAcceptanceLink))


def identifier(observation_id): return str(uuid5(NAMESPACE, str(UUID(observation_id))))


def seal(db, body, *, domain=SCHEMA):
    return hmac.new(_existing_salt(db), (domain+':').encode()+_encoded(body)[0].encode(), hashlib.sha256).hexdigest()


def _custody(db, order, observation, plan, attested):
    body = {'observation_id': observation.id, 'observation_digest': observation.payload_digest,
        'order_id': order.id, 'order_digest': order.payload_digest, 'root_account_id': plan['review']['claim']['account_id'],
        'custody': _encoded(plan['custody'])[0]}
    expected = hmac.new(_existing_salt(db), b'orbis-external-custody-v1:'+_encoded(body)[0].encode(), hashlib.sha256).hexdigest()
    bindings = [{key: item[key] for key in ('address_ref', 'account_id', 'role')} for item in plan['custody']]
    if (not hmac.compare_digest(attested, expected) or bindings != plan['review']['bindings']):
        raise ProjectionError('La declaración perdió su custodia original autenticada')


def _claim(order, body):
    args = review_arguments(body['plan']['review'], body['observation_json'])
    claim = args['claim']; proof = order[1]['proof']; row = order[0]
    if (claim.source_kind != 'EXTERNAL_OBSERVATION' or claim.source_id != row.observation_id
            or claim.order_record_id != row.id or claim.order_record_digest != row.payload_digest
            or claim.context_account_id != row.account_id or claim.account_id != body['account_id']
            or any(getattr(claim, key) != int(proof[key]) for key in ('nonce','block_number','transaction_index','block_transaction_count'))
            or claim.block_at != _time(datetime.fromisoformat(proof['block_at']))
            or any(getattr(claim, key) != proof[key] for key in ('block_hash','block_transactions_ref','signer_digest'))):
        raise ProjectionError('El booking externo perdió su certificado de orden')
    return claim, args


def booking_record(db: Session, record_id: str):
    from app.portfolio.economic_verified_read import record
    return record(db, ('external-booking', record_id), lambda: _read_booking(db, record_id))


def _read_booking(db: Session, record_id: str):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() < 78: return None
    row = db.get(m.RealDexExternalBooking, str(UUID(record_id)))
    if row is None: return None
    try:
        body = _decoded(row.payload_json, row.payload_digest)
        if (set(body) != {'schema','id','observation_id','order_id','account_id','observation_json','plan','plan_digest',
                'custody_seal','declaration_seal','booked_at','seal'} or body['schema'] != SCHEMA
                or body['id'] != row.id or row.id != identifier(row.observation_id)
                or any(body[key] != getattr(row,key) for key in ('observation_id','order_id','account_id'))
                or body['booked_at'] != row.created_at.isoformat()
                or body['plan_digest'] != _encoded(body['plan'])[1]
                or not hmac.compare_digest(body['seal'],seal(db,{k:v for k,v in body.items() if k!='seal'}))
                or not hmac.compare_digest(body['declaration_seal'],seal(db,body['plan'],domain='orbis.external.declaration.v1'))):
            raise ValueError('identity/seals')
        order = orders.order_record(db,row.order_id)
        observed = observations.observation_record(db,row.observation_id)
        if order is None or observed is None: raise ValueError('source')
        if (order[0].observation_id != row.observation_id or row.created_at < order[0].created_at
                or body['observation_json'] != _encoded(observed[1]['proof'])[0]): raise ValueError('source facts')
        plan = body['plan']; review = plan['review']
        from app.portfolio.economic_external_plan import SCHEMA as PLAN, BATCH_MEMBER_SCHEMA, event_id, request_id
        if (plan['schema'] not in (PLAN,BATCH_MEMBER_SCHEMA) or plan['recorded_at'] != body['booked_at']
                or not review['manual_review_complete'] or any(item['disposition']=='UNKNOWN' for item in review['annotations'])):
            raise ValueError('incomplete review')
        if plan['schema']==BATCH_MEMBER_SCHEMA and (db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<79
                or not isinstance(plan.get('batch_ref'),str) or len(plan['batch_ref'])!=64
                or any(c not in '0123456789abcdef'for c in plan['batch_ref'])):
            raise ValueError('joint declaration profile')
        _custody(db,order[0],observed[0],plan,body['custody_seal'])
        claim,args = _claim(order,body)
        rows = list(db.scalars(select(m.RealDexExternalBookingMember).where(
            m.RealDexExternalBookingMember.booking_id==row.id).order_by(m.RealDexExternalBookingMember.ordinal)))
        if [(item.event_id,item.role,item.ordinal) for item in rows] != [(item.event_id,item.role,n) for n,item in enumerate(claim.members)]:
            raise ValueError('members')
        accounts = {item['account_id'] for item in plan['custody']}
        if any(db.get(m.RealAccount,account)is None for account in accounts): raise ValueError('missing custody account')
        if set(db.scalars(select(m.RealDexExternalBookingAccount.account_id).where(
                m.RealDexExternalBookingAccount.booking_id==row.id))) != accounts or row.account_id not in accounts:
            raise ValueError('accounts')
        events = []; refs = []
        for member in claim.members:
            original = db.get(m.RealEvent,member.event_id)
            if original is None or original.account_id != member.account_id: raise ValueError('event')
            event = _from_row(original)
            if original.payload_digest != _digest(event.input) or event.recorded_at > row.created_at: raise ValueError('event digest/time')
            events.append(event); refs.append({'id':event.id,'account_id':event.account_id,'request_id':event.request_id,
                'recorded_at':event.recorded_at.isoformat(),'input_digest':_digest(event.input),'role':member.role})
        if refs != review['selected_events']: raise ValueError('selected events')
        for item, spec in zip(plan['created_events'],plan['created_event_keys'],strict=True):
            event = next((value for value in events if value.id==item['id']),None)
            if (event is None or event.id != event_id(plan['review_id'],spec['key']) or event.account_id != spec['account_id']
                    or event.request_id != request_id(plan['review_id'],spec['pair_key'] or spec['key'],paired=spec['pair_key'] is not None)
                    or event.recorded_at != row.created_at or item != {key:value for key,value in next(ref for ref in refs if ref['id']==event.id).items() if key!='role'}):
                raise ValueError('created original')
        used = set()
        for cls in (m.RealWalletSwapBookingEvent,m.RealDexReceiptBookingEvent,m.RealMexcFillBooking):
            used.update(db.scalars(select(cls.event_id).where(cls.event_id.in_(claim.event_ids))))
        if used: raise ValueError('original allocated to another source')
        validate_external_effect_units(tuple(events),**args)
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError,StopIteration):
        raise ProjectionError('El booking externo cambió o perdió su declaración y originales') from None
    return row,body


def witness_body(db, record):
    row,body = record; order = orders.order_record(db,row.order_id)[0]
    return {'schema':WITNESS,'source_kind':'EXTERNAL_OBSERVATION','source_id':row.observation_id,
        'order_record_id':order.id,'order_record_digest':order.payload_digest,'context_account_id':order.account_id,
        'account_id':row.account_id,'owner_digest':order.wallet_digest,'booking_id':row.id,'booking_digest':row.payload_digest,
        'events':body['plan']['review']['selected_events']}


def archived_witness(db, observation_id):
    record = booking_record(db,identifier(observation_id))
    if record is None: raise ProjectionError('Falta el booking externo revisado')
    body = witness_body(db,record)
    return {**body,'seal':seal(db,body,domain=WITNESS)}


def associated_bookings(db, accounts, *, recorded_through=None):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<78: return ()
    ids=set()
    from app.portfolio.economic_projection_review import _chunks
    for batch in _chunks(accounts):
        ids.update(db.scalars(select(m.RealDexExternalBookingAccount.booking_id).where(
            m.RealDexExternalBookingAccount.account_id.in_(batch))))
    result=[]
    for record_id in sorted(ids):
        record=booking_record(db,record_id)
        if record is None: raise ProjectionError('El vínculo de cuentas perdió su booking externo')
        if recorded_through is None or record[0].created_at<=recorded_through: result.append(record)
    return tuple(result)


def capture_external_witness(db, order_id, *, wallet_address):
    from app.portfolio import dex_call_resolution as calls
    from app.portfolio.wallet_funding_credit import _wallet_owner
    from app.portfolio.wallet_proof import canonical_wallet_address
    order=orders.order_record(db,order_id)
    if order is None: raise ProjectionError('Falta el certificado externo')
    observation=observations.observation_record(db,order[0].observation_id)
    original=observation[1]; wallet=canonical_wallet_address(wallet_address)
    calls._context(db,original['source_kind'],original['source_id'],order[0].account_id,wallet)
    record=booking_record(db,identifier(order[0].observation_id));owner=_wallet_owner(db,wallet)
    if record is None or owner is None or owner.id!=record[0].account_id:
        raise ProjectionError('El booking externo pertenece a otra custodia')
    return archived_witness(db,record[0].observation_id)


def claim_from_external_witness(db,witness):
    body = {key:value for key,value in witness.items() if key!='seal'}
    if (body.get('schema') != WITNESS or not hmac.compare_digest(witness.get('seal',''),seal(db,body,domain=WITNESS))
            or archived_witness(db,body['source_id']) != witness):
        raise ProjectionError('La prueba contable externa perdió su booking')
    record = booking_record(db,body['booking_id'])
    return _claim(orders.order_record(db,record[0].order_id),record[1])[0]


def materialize_external_booking(db, inputs, plan, *, batch_ref=None):
    """Private composable writer. Caller must revalidate, accept, link and commit."""
    if not db.connection().connection.driver_connection.in_transaction:
        raise ProjectionError('Falta la transacción de aceptación externa')
    body_plan = json.loads(plan.body_json)
    from app.portfolio.economic_external_plan import SCHEMA as PLAN, BATCH_MEMBER_SCHEMA
    if ((batch_ref is None and body_plan['schema']!=PLAN)
            or batch_ref is not None and (body_plan['schema']!=BATCH_MEMBER_SCHEMA or body_plan.get('batch_ref')!=batch_ref
                or db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<79)):
        raise ProjectionError('El plan externo necesita su puerto de aceptación correspondiente')
    if (body_plan['state_digest'] != inputs.state_digest or body_plan['custody'] != json.loads(inputs.custody_json)
            or not plan.review.body['manual_review_complete']):
        raise ProjectionError('Revisa los efectos desconocidos antes de aceptar')
    for event in plan.created_events:
        if (db.get(m.RealEvent,event.id) is not None or db.scalar(select(m.RealEvent.id).where(
                m.RealEvent.account_id==event.account_id,m.RealEvent.request_id==event.request_id)) is not None):
            raise ProjectionError('El asiento propuesto ya existe')
        item=event.input
        db.add(m.RealEvent(id=event.id,account_id=event.account_id,request_id=event.request_id,payload_digest=_digest(item),
            kind=item.kind,asset=item.asset,quote_currency=item.quote_currency,quantity=item.quantity,unit_price=item.unit_price,
            gross_amount=item.gross_amount,fee=item.fee,fee_asset=item.fee_asset,occurred_at=item.occurred_at,
            recorded_at=event.recorded_at,note=item.note))
    db.flush(); stamp=_time(datetime.fromisoformat(body_plan['recorded_at'])); record_id=identifier(inputs.observation_id)
    body={'schema':SCHEMA,'id':record_id,'observation_id':inputs.observation_id,'order_id':inputs.order_id,
        'account_id':inputs.root_account_id,'observation_json':inputs.observation_json,'plan':body_plan,'plan_digest':plan.digest,
        'custody_seal':inputs.custody_seal,'declaration_seal':seal(db,body_plan,domain='orbis.external.declaration.v1'),'booked_at':stamp.isoformat()}
    raw,digest=_encoded({**body,'seal':seal(db,body)})
    db.add(m.RealDexExternalBooking(id=record_id,observation_id=inputs.observation_id,order_id=inputs.order_id,
        account_id=inputs.root_account_id,payload_json=raw,payload_digest=digest,created_at=stamp));db.flush()
    for ordinal,member in enumerate(plan.review.body['claim']['members']):
        db.add(m.RealDexExternalBookingMember(booking_id=record_id,event_id=member['event_id'],role=member['role'],ordinal=ordinal))
    for account in sorted({item.account_id for item in inputs.bindings}):
        db.add(m.RealDexExternalBookingAccount(booking_id=record_id,account_id=account))
    db.flush();return archived_witness(db,inputs.observation_id)


def verify_all_external_bookings(db,*,version):
    if version<78:
        for name in TABLES:
            exists=db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(name,)).first()
            if exists and db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one():
                raise ProjectionError('Una bóveda antigua contiene revisiones externas ocultas')
        return
    from app.portfolio.economic_external_acceptance import accepted_link
    for record_id in db.scalars(select(m.RealDexExternalBooking.id)):
        booking_record(db,record_id);accepted_link(db,record_id)
    for cls in (m.RealDexExternalBookingMember,m.RealDexExternalBookingAccount,m.RealDexExternalAcceptanceLink):
        for item in db.scalars(select(cls)):
            if booking_record(db,item.booking_id) is None: raise ProjectionError('Miembro financiero externo sin booking')
