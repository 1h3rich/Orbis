"""Authenticated joint manifests and first-acceptance links, without replay I/O.

The manifest references bookings; the later group references the criterion;
member seals reference the group. No hash points back to a later hash.
"""
from __future__ import annotations

from datetime import datetime
import hmac

from sqlalchemy import select

from app.portfolio import models as m,economic_acceptance as archive,economic_external_booking as booking
from app.portfolio.economic_external_batch import SCHEMA as BATCH,MAX_DECLARATIONS
from app.portfolio.economic_external_plan import BATCH_MEMBER_SCHEMA
from app.portfolio.economic_external_effects import review_arguments,review_external_effects
from app.portfolio.economic_projection import ProjectionError,ProjectionLimits,TieOrder,_uuid
from app.portfolio.wallet_funding import _encoded,_decoded,_time

MANIFEST='orbis.portfolio.external.effects.group.manifest.v1'
GROUP='orbis.portfolio.external.acceptance.group.v1'
LINK='orbis.portfolio.external.acceptance.group.link.v1'
TABLES=(m.RealDexExternalGroupLink.__tablename__,m.RealDexExternalAcceptanceGroup.__tablename__)


def validate_manifest(db,manifest,review):
    try:
        if set(manifest)!={'schema','group_id','batch_plan','members','selection_digest'}or manifest['schema']!=MANIFEST:
            raise ValueError('manifest')
        _uuid(manifest['group_id']);archive.identifier(manifest['selection_digest'])
        batch=manifest['batch_plan'];members=manifest['members']
        if (batch['schema']!=BATCH or batch['batch_id']!=manifest['group_id']
                or batch['batch_ref']!=_encoded(batch['selection'])[1]or not batch['manual_review_complete']
                or batch['root_account_id']!=review['root_account_id']or not 2<=len(members)<=MAX_DECLARATIONS
                or batch['accounts']!=sorted(review['account_revisions'])
                or any(batch[key]is not False for key in('saved','acceptance_available','accounting_acceptance_active','moves_funds','strategy_executed','resolves_intents','trading_authority'))):
            raise ValueError('batch')
        expected=[];created=[];observations=[];selections=[]
        for member in members:
            if set(member)!={'booking_id','booking_digest','plan','observation_json'}:raise ValueError('member')
            row,body=booking.booking_record(db,member['booking_id'])
            plan=member['plan'];witness=booking.archived_witness(db,row.observation_id)
            if (row.payload_digest!=member['booking_digest']or body['plan']!=plan
                    or body['observation_json']!=member['observation_json']or witness not in review['witnesses']
                    or plan['schema']!=BATCH_MEMBER_SCHEMA or plan['batch_ref']!=batch['batch_ref']
                    or plan['recorded_at']!=batch['recorded_at']
                    or plan['review']['projection_digest']!=batch['projection_digest']
                    or plan['review']['ledger_input_digest']!=batch['ledger_input_digest']):raise ValueError('declaration')
            observations.append(row.observation_id)
            expected.append({'observation_id':row.observation_id,'digest':_encoded(plan)[1]})
            created.extend(event['id']for event in plan['created_events'])
            selections.append((row.observation_id,plan['state_digest'],plan['review_id']))
        actual=[(item['observation_id'],item['state_digest'],item['review_id'])for item in batch['selection']['declarations']]
        if (observations!=sorted(set(observations))or batch['plans']!=expected or actual!=selections
                or batch['created_event_ids']!=sorted(created)or len(set(created))!=len(created)):
            raise ValueError('membership')
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError):
        raise ProjectionError('La aceptación conjunta perdió sus declaraciones y fuentes')from None


def replay_manifest(inputs,manifest):
    for member in manifest['members']:
        body=member['plan']['review'];args=review_arguments(body,member['observation_json'])
        prior=tuple(item for item in inputs.claims if item.order_record_id!=body['order_record_id'])
        reviewed=review_external_effects(inputs.events,prior,**args,limits=ProjectionLimits(**body['limits']),
            tie_orders=tuple(TieOrder(item['account_id'],datetime.fromisoformat(item['at']),tuple(item['nodes']))for item in body['tie_orders']))
        if reviewed.body!=body:raise ProjectionError('El conjunto no se reproduce desde los originales y recibos')


def group_record(db,group_id):
    from app.portfolio.economic_verified_read import record
    return record(db, ('external-group', group_id), lambda: _read_group(db, group_id))


def _read_group(db,group_id):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<79:return None
    row=db.get(m.RealDexExternalAcceptanceGroup,_uuid(group_id))
    if row is None:return None
    try:
        body=_decoded(row.payload_json,row.payload_digest)
        accepted,payload=archive._header(db,row.acceptance_id)
        if payload['draft']['schema']!=archive.EXTERNAL_GROUP_DRAFT:raise ValueError('profile')
        manifest=payload['draft']['external_group']
        wanted=[{'booking_id':item['booking_id'],'booking_digest':item['booking_digest'],'ordinal':index}
            for index,item in enumerate(manifest['members'])]
        if (set(body)!={'schema','id','acceptance_id','acceptance_digest','batch_ref','members','recorded_at','seal'}
                or body['schema']!=GROUP or body['id']!=row.id or manifest['group_id']!=row.id
                or body['acceptance_id']!=row.acceptance_id or body['acceptance_digest']!=accepted.payload_digest
                or body['batch_ref']!=manifest['batch_plan']['batch_ref']or body['members']!=wanted
                or body['recorded_at']!=row.created_at.isoformat()or row.created_at!=accepted.created_at
                or not hmac.compare_digest(body['seal'],booking.seal(db,{k:v for k,v in body.items()if k!='seal'},domain=GROUP))):
            raise ValueError('group')
        links=list(db.scalars(select(m.RealDexExternalGroupLink).where(m.RealDexExternalGroupLink.group_id==row.id).order_by(m.RealDexExternalGroupLink.ordinal)))
        if len(links)!=len(wanted):raise ValueError('lost member')
        for link,spec in zip(links,wanted,strict=True):_validate_link(db,link,row,body,spec)
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError):
        raise ProjectionError('El grupo externo cambió o perdió su aceptación y miembros')from None
    return row,body


def _validate_link(db,row,group,group_body,spec):
    body=_decoded(row.payload_json,row.payload_digest);record=booking.booking_record(db,row.booking_id)
    if (record is None or db.get(m.RealDexExternalAcceptanceLink,row.booking_id)is not None
            or set(body)!={'schema','booking_id','booking_digest','group_id','group_digest','acceptance_id','acceptance_digest','ordinal','recorded_at','seal'}
            or body['schema']!=LINK or row.booking_id!=spec['booking_id']or row.ordinal!=spec['ordinal']
            or body['booking_id']!=row.booking_id or body['booking_digest']!=spec['booking_digest']
            or record[0].payload_digest!=spec['booking_digest']or record[1]['plan']['batch_ref']!=group_body['batch_ref']
            or row.group_id!=group.id or body['group_id']!=group.id or body['group_digest']!=group.payload_digest
            or row.acceptance_id!=group.acceptance_id or body['acceptance_id']!=row.acceptance_id
            or body['acceptance_digest']!=group_body['acceptance_digest']or body['ordinal']!=row.ordinal
            or row.created_at!=group.created_at or row.created_at<record[0].created_at
            or body['recorded_at']!=row.created_at.isoformat()
            or not hmac.compare_digest(body['seal'],booking.seal(db,{k:v for k,v in body.items()if k!='seal'},domain=LINK))):
        raise ProjectionError('El enlace conjunto perdió su booking, grupo o criterio')
    return body


def accepted_group_link(db,booking_id,*,booking_record=None):
    row=db.get(m.RealDexExternalGroupLink,booking_id)
    if row is None:raise ProjectionError('El booking perdió su enlace conjunto')
    record=group_record(db,row.group_id)
    if record is None:raise ProjectionError('El enlace perdió su grupo financiero')
    # group_record authenticates every member, including this row's seal.
    return row,_decoded(row.payload_json,row.payload_digest)


def result(db,group_id,*,created):
    row,body=group_record(db,group_id);accepted,payload=archive._header(db,row.acceptance_id)
    manifest=payload['draft']['external_group'];heads=archive._heads(db,tuple(payload['draft']['prefix']))
    count=len(manifest['batch_plan']['created_event_ids'])
    return {**archive._result(accepted,created=created),'group_id':row.id,'booking_ids':[item['booking_id']for item in body['members']],
        'selection_digest':manifest['selection_digest'],'preview_digest':accepted.draft_digest,
        'is_current':all(value is not None and value['id']==row.acceptance_id for value in heads.values()),
        'external_review_accepted':True,'joint_review':True,'events_created':count if created else 0,'new_events_count':count,
        'effects_proven_complete':False,'strategy_executed':False,'resolves_intent':False,'moves_funds':False,'trading_authority':False}


def verify_all_groups(db,*,version):
    if version<79:
        for name in TABLES:
            exists=db.connection().exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).first()
            if exists and db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one():
                raise ProjectionError('Una bóveda antigua contiene grupos financieros ocultos')
        return
    for group_id in db.scalars(select(m.RealDexExternalAcceptanceGroup.id)):group_record(db,group_id)
    for row in db.scalars(select(m.RealDexExternalGroupLink)):
        if group_record(db,row.group_id)is None:raise ProjectionError('Hay un miembro financiero sin grupo')
    # A missing group AND all links cannot hide an already archived manifest.
    for record_id in db.scalars(select(m.RealEconomicAcceptance.id)):
        _,body=archive._header(db,record_id)
        if body['draft']['schema']==archive.EXTERNAL_GROUP_DRAFT:
            record=group_record(db,body['draft']['external_group']['group_id'])
            if record is None or record[0].acceptance_id!=record_id:raise ProjectionError('El criterio perdió su grupo financiero')
