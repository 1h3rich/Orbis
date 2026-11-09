"""One atomic external booking + original events + shared accounting criterion.

Staging rolls back. Financial replay happens outside leases. Original intents
remain immutable and no accepted manual interpretation becomes a strategy fill.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import hmac

from sqlalchemy.orm import Session

from app.portfolio import economic_acceptance as archive, models as m
from app.portfolio import economic_external_booking as booking
from app.portfolio.economic_external_effects import review_arguments, review_external_effects
from app.portfolio.economic_projection import ProjectionError, ProjectionLimits, TieOrder
from app.portfolio.economic_projection_review import capture_projection_inputs
from app.portfolio.economic_projection_review import ProjectionInputs
from app.portfolio.economic_external_capture import ExternalEffectsInputs
from app.portfolio.economic_external_plan import ExternalEffectsPlan
from app.portfolio.wallet_funding import _decoded, _encoded, _time

MANIFEST = 'orbis.portfolio.external.effects.acceptance.manifest.v1'


def validate_external_manifest(db, manifest, review):
    try:
        if set(manifest)!={'schema','booking_id','booking_digest','plan','observation_json','selection_digest'} or manifest['schema']!=MANIFEST:
            raise ValueError('manifest')
        archive.identifier(manifest['selection_digest'])
        record=booking.booking_record(db,manifest['booking_id'])
        if record is None or record[0].payload_digest!=manifest['booking_digest']:
            raise ValueError('booking')
        row,body=record
        from app.portfolio.economic_external_plan import SCHEMA as PLAN
        if body['plan']['schema']!=PLAN:raise ValueError('single declaration profile')
        witness=booking.archived_witness(db,row.observation_id)
        if (manifest['plan']!=body['plan'] or manifest['observation_json']!=body['observation_json']
                or witness not in review['witnesses'] or review['root_account_id']!=row.account_id):
            raise ValueError('declaration/witness')
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError):
        raise ProjectionError('El criterio externo perdió su declaración autenticada') from None


def replay_external_manifest(inputs, manifest):
    body=manifest['plan']['review'];args=review_arguments(body,manifest['observation_json'])
    prior=tuple(item for item in inputs.claims if item.order_record_id!=body['order_record_id'])
    reviewed=review_external_effects(inputs.events,prior,**args,
        limits=ProjectionLimits(**body['limits']),tie_orders=tuple(TieOrder(item['account_id'],
            datetime.fromisoformat(item['at']),tuple(item['nodes']))for item in body['tie_orders']))
    if reviewed.body!=body:
        raise ProjectionError('La declaración financiera externa no se reproduce desde sus originales')


@dataclass(frozen=True)
class StagedExternal:
    inputs: ProjectionInputs
    parents_json: str
    manifest_json: str
    operating_json: str


@dataclass(frozen=True)
class PreparedExternal:
    captured: ExternalEffectsInputs
    plan: ExternalEffectsPlan
    staged: StagedExternal
    draft: dict
    digest: str


def _materialize(db,captured,plan,selection_digest):
    witness=booking.materialize_external_booking(db,captured,plan)
    projected=capture_projection_inputs(db,captured.root_account_id,[*map(json.loads,captured.witness_json),witness],limits=captured.limits)
    if set(projected.accounts)!=set(captured.accounts):
        raise ProjectionError('La revisión perdió cuentas al unir los historiales')
    expected=tuple(sorted([*captured.claims,review_arguments(plan.review.body,captured.observation_json)['claim']],key=lambda item:item.order_record_id))
    if tuple(sorted(projected.claims,key=lambda item:item.order_record_id))!=expected:
        raise ProjectionError('La materialización cambió los miembros del orden revisado')
    record=booking.booking_record(db,witness['booking_id'])
    manifest={'schema':MANIFEST,'booking_id':record[0].id,'booking_digest':record[0].payload_digest,
        'plan':json.loads(plan.body_json),'observation_json':captured.observation_json,'selection_digest':selection_digest}
    from app.portfolio.economic_operational_guard import operational_report
    operating=operational_report(db,projected.accounts,[*map(json.loads,projected.witness_json)],
        now=_time(datetime.fromisoformat(json.loads(plan.body_json)['recorded_at'])))
    return StagedExternal(projected,_encoded(archive._heads(db,projected.accounts))[0],_encoded(manifest)[0],_encoded(operating)[0])


def prepare_external_acceptance(vault,captured,plan,*,capture,generation,selection_digest,tie_orders=()):
    with vault.guard_session_generation(generation),vault.session()as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            if capture(db).state_digest!=captured.state_digest:
                raise ProjectionError('El libro o la custodia cambió; revisa de nuevo')
            staged=_materialize(db,captured,plan,selection_digest)
        finally: db.rollback()
    draft,_=archive._draft(staged.inputs,json.loads(staged.parents_json),tie_orders)
    draft={**draft,'schema':archive.EXTERNAL_EFFECTS_DRAFT,'external_effects':json.loads(staged.manifest_json)}
    raw,digest=_encoded(draft)
    with vault.guard_session_generation(generation):
        return PreparedExternal(captured,plan,staged,json.loads(raw),digest)


def accepted_link(db: Session,booking_id):
    record=booking.booking_record(db,booking_id)
    if record is None: return None
    row=db.get(m.RealDexExternalAcceptanceLink,booking_id)
    grouped=db.get(m.RealDexExternalGroupLink,booking_id)if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=79 else None
    if grouped is not None:
        if row is not None:raise ProjectionError('El booking tiene dos aceptaciones incompatibles')
        from app.portfolio.economic_external_group import accepted_group_link
        return accepted_group_link(db,booking_id,booking_record=record)
    if row is None: raise ProjectionError('El booking externo perdió su aceptación atómica')
    try:
        from app.portfolio.economic_external_plan import SCHEMA as PLAN
        if record[1]['plan']['schema']!=PLAN:raise ValueError('single link profile')
        body=_decoded(row.payload_json,row.payload_digest)
        acceptance,payload=archive._header(db,row.acceptance_id)
        if (set(body)!={'schema','booking_id','booking_digest','acceptance_id','acceptance_digest','recorded_at','seal'}
                or body['schema']!=booking.LINK or body['booking_id']!=row.booking_id
                or body['booking_digest']!=record[0].payload_digest or body['acceptance_id']!=row.acceptance_id
                or body['acceptance_digest']!=acceptance.payload_digest or body['recorded_at']!=row.created_at.isoformat()
                or row.created_at!=acceptance.created_at or row.created_at<record[0].created_at
                or payload['draft']['schema']!=archive.EXTERNAL_EFFECTS_DRAFT
                or payload['draft']['external_effects']['booking_id']!=booking_id
                or not hmac.compare_digest(body['seal'],booking.seal(db,{k:v for k,v in body.items()if k!='seal'},domain=booking.LINK))):
            raise ValueError('link')
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError):
        raise ProjectionError('El cierre financiero externo cambió o perdió su criterio') from None
    return row,body


def result(db,record,*,created):
    if isinstance(record[0],m.RealDexExternalGroupLink):
        from app.portfolio.economic_external_group import result as group_result
        return {**group_result(db,record[0].group_id,created=created),'booking_id':record[0].booking_id}
    row,body=record;accepted,payload=archive._header(db,row.acceptance_id)
    manifest=payload['draft']['external_effects'];root=accepted.root_account_id
    current=archive._heads(db,tuple(payload['draft']['prefix']))
    count=len(manifest['plan']['created_events'])
    return {**archive._result(accepted,created=created),'booking_id':row.booking_id,
        'selection_digest':manifest['selection_digest'],'preview_digest':accepted.draft_digest,
        'is_current':all(item is not None and item['id']==row.acceptance_id for item in current.values()),
        'external_review_accepted':True,'events_created':count if created else 0,'new_events_count':count,
        'effects_proven_complete':False,'strategy_executed':False,'resolves_intent':False,'moves_funds':False}


def recover_external_acceptance(vault,observation_id,*,context_account_id,selection_digest=None):
    from app.portfolio.economic_verified_read import scope
    with vault.session()as db,scope(db):
        archive._snapshot(db);archive._registry(db)
        from app.portfolio import dex_external_observation as observations
        observed=observations.observation_record(db,observation_id)
        if observed is None or observed[0].account_id!=context_account_id: raise ProjectionError('Observación externa de otro contexto')
        record=accepted_link(db,booking.identifier(observation_id))
        if record is None:return None
        row,payload=archive._header(db,record[0].acceptance_id)
        manifest=payload['draft'].get('external_effects',payload['draft'].get('external_group'))
        if selection_digest is not None and manifest['selection_digest']!=selection_digest:
            raise ProjectionError('La respuesta guardada pertenece a otra declaración')
        archived=archive.capture_acceptance(db,row.id)
    archive.replay_acceptance(archived)
    with vault.session()as db,scope(db):
        archive._snapshot(db)
        return result(db,accepted_link(db,booking.identifier(observation_id)),created=False)


def confirm_external_acceptance(vault,prepared,*,capture,preview_digest,now,generation,commit_guard=None,wallet_addresses=()):
    now=_time(now);stamp=_time(datetime.fromisoformat(prepared.plan.review.body.get('recorded_at') or json.loads(prepared.plan.body_json)['recorded_at']))
    if prepared.digest!=preview_digest or now<stamp or now-stamp>timedelta(minutes=15):
        raise ProjectionError('La revisión externa cambió o caducó; revisa de nuevo')
    with (commit_guard or nullcontext)(),vault.guard_session_generation(generation),vault.session()as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            if db.get(m.RealDexExternalBooking,booking.identifier(prepared.captured.observation_id))is not None:
                existing=accepted_link(db,booking.identifier(prepared.captured.observation_id))
                accepted,payload=archive._header(db,existing[0].acceptance_id)
                if accepted.draft_digest!=preview_digest: raise ProjectionError('Ya existe otra revisión para el recibo')
                saved=result(db,existing,created=False);db.commit();return saved
            if capture(db).state_digest!=prepared.captured.state_digest:
                raise ProjectionError('El libro, el criterio o los vínculos cambiaron durante la revisión')
            staged=_materialize(db,prepared.captured,prepared.plan,json.loads(prepared.staged.manifest_json)['selection_digest'])
            if staged!=prepared.staged: raise ProjectionError('Los originales materializados no conservan la revisión')
            archive._store_reviewed_draft(db,prepared.captured.root_account_id,prepared.draft,preview_digest,
                now=now,not_before=stamp,wallet_addresses=wallet_addresses)
            acceptance,_=archive._header(db,archive.identifier(preview_digest));booking_id=json.loads(staged.manifest_json)['booking_id']
            body={'schema':booking.LINK,'booking_id':booking_id,'booking_digest':json.loads(staged.manifest_json)['booking_digest'],
                'acceptance_id':acceptance.id,'acceptance_digest':acceptance.payload_digest,'recorded_at':now.isoformat()}
            raw,digest=_encoded({**body,'seal':booking.seal(db,body,domain=booking.LINK)})
            db.add(m.RealDexExternalAcceptanceLink(booking_id=booking_id,acceptance_id=acceptance.id,payload_json=raw,payload_digest=digest,created_at=now));db.flush()
            saved=result(db,accepted_link(db,booking_id),created=True);db.commit();return saved
        except Exception:db.rollback();raise
