"""Atomic joint financial acceptance. No RPC, wallet signing or worker start."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime,timedelta
import json

from app.portfolio import models as m,economic_acceptance as archive,economic_external_booking as booking
from app.portfolio import economic_external_group as groups
from app.portfolio.economic_external_batch import ExternalBatchPlan,SCHEMA as BATCH
from app.portfolio.economic_external_capture import ExternalEffectsInputs
from app.portfolio.economic_external_effects import review_arguments
from app.portfolio.economic_projection import ProjectionError
from app.portfolio.economic_projection_review import ProjectionInputs,capture_projection_inputs
from app.portfolio.wallet_funding import _encoded,_time
from app.portfolio.economic_verified_read import scope as verified_scope


@dataclass(frozen=True)
class StagedGroup:
    inputs:ProjectionInputs
    parents_json:str
    manifest_json:str
    operating_json:str


@dataclass(frozen=True)
class PreparedGroup:
    captured:tuple[ExternalEffectsInputs,...]
    plan:ExternalBatchPlan
    staged:StagedGroup
    draft_json:str
    digest:str


def _captures(values):
    values=tuple(sorted(values,key=lambda item:item.observation_id))
    if not 2<=len(values)<=256 or len({item.observation_id for item in values})!=len(values):
        raise ProjectionError('El conjunto perdió sus capturas de origen')
    return values


def _unchanged(db,expected,capture):
    current=_captures(capture(db))
    if [(item.observation_id,item.state_digest)for item in current]!=[(item.observation_id,item.state_digest)for item in expected]:
        raise ProjectionError('Cambió una cartera, fuente o criterio del conjunto; revisa de nuevo')


def _materialize(db,captured,plan,selection_digest,*,wallet_addresses):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<79:raise ProjectionError('Actualiza la bóveda para aceptar grupos externos')
    body=json.loads(plan.body_json)
    if body['schema']!=BATCH or _encoded(body)[1]!=plan.digest or not body['manual_review_complete']:
        raise ProjectionError('Completa la revisión manual de todos los recibos')
    if body['plans']!=[{'observation_id':item.observation_id,'digest':part.digest}for item,part in zip(captured,plan.plans,strict=True)]:
        raise ProjectionError('El plan conjunto perdió sus capturas o declaraciones')
    members=[];witnesses={json.loads(raw)['order_record_id']:json.loads(raw)for raw in captured[0].witness_json}
    claims=list(captured[0].claims)
    for item,part in zip(captured,plan.plans,strict=True):
        witness=booking.materialize_external_booking(db,item,part,batch_ref=body['batch_ref'])
        witnesses[witness['order_record_id']]=witness
        claims.append(review_arguments(part.review.body,item.observation_json)['claim'])
        row,payload=booking.booking_record(db,witness['booking_id'])
        members.append({'booking_id':row.id,'booking_digest':row.payload_digest,
            'plan':payload['plan'],'observation_json':payload['observation_json']})
    projected=capture_projection_inputs(db,body['root_account_id'],list(witnesses.values()),limits=captured[0].limits)
    if (set(projected.accounts)!=set(body['accounts'])or tuple(sorted(projected.claims,key=lambda item:item.order_record_id))
            !=tuple(sorted(claims,key=lambda item:item.order_record_id))):raise ProjectionError('La materialización perdió cuentas o fuentes del conjunto')
    manifest={'schema':groups.MANIFEST,'group_id':body['batch_id'],'batch_plan':body,'members':members,'selection_digest':selection_digest}
    from app.portfolio.economic_operational_guard import operational_report
    operating=operational_report(db,projected.accounts,list(witnesses.values()),now=_time(datetime.fromisoformat(body['recorded_at'])),wallet_addresses=wallet_addresses)
    return StagedGroup(projected,_encoded(archive._heads(db,projected.accounts))[0],_encoded(manifest)[0],_encoded(operating)[0])


def prepare_group(vault,captured,plan,*,capture,generation,selection_digest,wallet_addresses=(),tie_orders=()):
    captured=_captures(captured);archive.identifier(selection_digest)
    with vault.guard_session_generation(generation),vault.session()as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE');_unchanged(db,captured,capture)
            staged=_materialize(db,captured,plan,selection_digest,wallet_addresses=wallet_addresses)
        finally:db.rollback()
    draft,_=archive._draft(staged.inputs,json.loads(staged.parents_json),tie_orders)
    draft={**draft,'schema':archive.EXTERNAL_GROUP_DRAFT,'external_group':json.loads(staged.manifest_json)}
    raw,digest=_encoded(draft)
    with vault.guard_session_generation(generation):return PreparedGroup(captured,plan,staged,raw,digest)


def _store_group(db,manifest,acceptance,*,now):
    members=[{'booking_id':item['booking_id'],'booking_digest':item['booking_digest'],'ordinal':index}
        for index,item in enumerate(manifest['members'])]
    body={'schema':groups.GROUP,'id':manifest['group_id'],'acceptance_id':acceptance.id,
        'acceptance_digest':acceptance.payload_digest,'batch_ref':manifest['batch_plan']['batch_ref'],
        'members':members,'recorded_at':now.isoformat()}
    raw,digest=_encoded({**body,'seal':booking.seal(db,body,domain=groups.GROUP)})
    db.add(m.RealDexExternalAcceptanceGroup(id=body['id'],acceptance_id=acceptance.id,
        payload_json=raw,payload_digest=digest,created_at=now));db.flush()
    links=[]
    for spec in members:
        member={'schema':groups.LINK,**spec,'group_id':body['id'],'group_digest':digest,
            'acceptance_id':acceptance.id,'acceptance_digest':acceptance.payload_digest,'recorded_at':now.isoformat()}
        value,sealed=_encoded({**member,'seal':booking.seal(db,member,domain=groups.LINK)})
        links.append(m.RealDexExternalGroupLink(booking_id=spec['booking_id'],group_id=body['id'],acceptance_id=acceptance.id,
            ordinal=spec['ordinal'],payload_json=value,payload_digest=sealed,created_at=now))
    db.add_all(links);db.flush()


def confirm_group(vault,prepared,*,capture,preview_digest,now,generation,commit_guard=None,wallet_addresses=()):
    now=_time(now);body=json.loads(prepared.plan.body_json);stamp=_time(datetime.fromisoformat(body['recorded_at']))
    if preview_digest!=prepared.digest or now<stamp or now-stamp>timedelta(minutes=15):
        raise ProjectionError('La revisión conjunta cambió o caducó')
    with(commit_guard or nullcontext)(),vault.guard_session_generation(generation),vault.session()as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            prior=groups.group_record(db,body['batch_id'])
            if prior is not None:
                accepted,_=archive._header(db,prior[0].acceptance_id)
                if accepted.draft_digest!=preview_digest:raise ProjectionError('El grupo ya tiene otra revisión')
                result=groups.result(db,prior[0].id,created=False);db.commit();return result
            if any(db.get(m.RealDexExternalBooking,booking.identifier(item.observation_id))is not None for item in prepared.captured):
                raise ProjectionError('Un recibo ya pertenece a otra aceptación o perdió su grupo')
            _unchanged(db,prepared.captured,capture)
            manifest=json.loads(prepared.staged.manifest_json)
            staged=_materialize(db,prepared.captured,prepared.plan,manifest['selection_digest'],wallet_addresses=wallet_addresses)
            if staged!=prepared.staged:raise ProjectionError('La materialización conjunta cambió desde la revisión')
            draft=json.loads(prepared.draft_json)
            archive._store_reviewed_draft(db,body['root_account_id'],draft,preview_digest,now=now,not_before=stamp,wallet_addresses=wallet_addresses)
            accepted,_=archive._header(db,archive.identifier(preview_digest));_store_group(db,manifest,accepted,now=now)
            result=groups.result(db,body['batch_id'],created=True);db.commit();return result
        except Exception:db.rollback();raise


def recover_group(vault,group_id,*,root_account_id,selection_digest=None):
    generation=vault.session_generation
    with vault.guard_session_generation(generation),vault.session()as db,verified_scope(db):
        archive._snapshot(db);archive._registry(db);record=groups.group_record(db,group_id)
        if record is None:return None
        row,payload=archive._header(db,record[0].acceptance_id);manifest=payload['draft']['external_group']
        if row.root_account_id!=root_account_id or selection_digest is not None and selection_digest!=manifest['selection_digest']:
            raise ProjectionError('El grupo pertenece a otra cartera o declaración')
        archived=archive.capture_acceptance(db,row.id)
    archive.replay_acceptance(archived)
    with vault.guard_session_generation(generation),vault.session()as db,verified_scope(db):
        archive._snapshot(db)
        return groups.result(db,group_id,created=False)
