"""Pure joint proposal from authenticated captures; never acceptance authority.

Each receipt keeps its own unit reconciliation. All proposed movements share
one chronology, including inventory received before a later returned transfer.
"""
from __future__ import annotations

from dataclasses import asdict,dataclass,replace
from datetime import datetime
from typing import Sequence

from app.portfolio.domain import PortfolioEvent
from app.portfolio.economic_external_capture import ExternalEffectsInputs
from app.portfolio.economic_external_effects import (AssetScale,EffectAllocation,EffectAnnotation,ManualEffect,
    review_external_effects,validate_external_effect_units)
from app.portfolio.economic_external_plan import (ExternalEffectsPlan,NewExternalEvent,_prepare_created,
    _claim_for,_review_args,_assembled_plan)
from app.portfolio.economic_projection import ExternalOrderMember,ProjectionError,ProjectionResult,TieOrder,_uuid
from app.portfolio.repository import _digest
from app.portfolio.wallet_funding import _encoded,_time

SCHEMA='orbis.portfolio.external.effects.batch.plan.v1'
MAX_DECLARATIONS=256


@dataclass(frozen=True)
class ExternalDeclaration:
    inputs:ExternalEffectsInputs
    review_id:str
    members:tuple[ExternalOrderMember,...]
    allocations:tuple[EffectAllocation,...]
    annotations:tuple[EffectAnnotation,...]
    new_events:tuple[NewExternalEvent,...]=()
    manual_effects:tuple[ManualEffect,...]=()
    asset_scales:tuple[AssetScale,...]=()

    def __post_init__(self):
        _uuid(self.review_id)
        if not isinstance(self.inputs,ExternalEffectsInputs):raise ProjectionError('Falta la captura de una declaración conjunta')
        for field,cls,maximum in (('members',ExternalOrderMember,8192),('allocations',EffectAllocation,4096),
                ('annotations',EffectAnnotation,4096),('new_events',NewExternalEvent,8192),
                ('manual_effects',ManualEffect,4096),('asset_scales',AssetScale,512)):
            values=getattr(self,field)
            if type(values)is not tuple or len(values)>maximum or any(not isinstance(item,cls)for item in values):
                raise ProjectionError('Una declaración conjunta supera sus límites o perdió su tipo')


@dataclass(frozen=True)
class ExternalBatchPlan:
    body_json:str
    digest:str
    plans:tuple[ExternalEffectsPlan,...]
    created_events:tuple[PortfolioEvent,...]
    projection:ProjectionResult


def _common_base(declarations):
    baseline=declarations[0].inputs
    fields=('accounts','events','claims','witness_json','currents','unavailable_event_ids','account_names','limits')
    for declaration in declarations:
        if any(getattr(declaration.inputs,key)!=getattr(baseline,key)for key in fields):
            raise ProjectionError('Las llamadas necesitan la misma captura de carteras, originales y criterios')
    if len({item.id for item in baseline.events})!=len(baseline.events):raise ProjectionError('El historial conjunto repite originales')
    return baseline


def _selection(declaration,created,claim):
    inputs=declaration.inputs
    return {'observation_id':inputs.observation_id,'observation_digest':inputs.observation_digest,
        'order_id':inputs.order_id,'order_digest':inputs.order_digest,'state_digest':inputs.state_digest,
        'custody_seal':inputs.custody_seal,'review_id':declaration.review_id,
        'members':[asdict(item)for item in claim.members],
        'new_events':[{'key':item.key,'pair_key':item.pair_key,'account_id':item.account_id,
            'allow_similar':item.allow_similar,'input_digest':_digest(item.input)}
            for item in sorted(declaration.new_events,key=lambda item:item.key)],
        'created_event_ids':[item.id for item in created],
        'allocations':[asdict(item)for item in declaration.allocations],
        'annotations':[asdict(item)for item in declaration.annotations],
        'manual_effects':[asdict(item)for item in declaration.manual_effects],
        'asset_scales':[asdict(item)for item in declaration.asset_scales]}


def prepare_external_batch(declarations:tuple[ExternalDeclaration,...],*,batch_id:str,root_account_id:str,
        recorded_at:datetime,tie_orders:Sequence[TieOrder]=())->ExternalBatchPlan:
    _uuid(batch_id);_uuid(root_account_id);stamp=_time(recorded_at)
    tie_orders=tuple(tie_orders)
    if (type(declarations)is not tuple or not 2<=len(declarations)<=MAX_DECLARATIONS
            or any(not isinstance(item,ExternalDeclaration)for item in declarations)):
        raise ProjectionError('Selecciona entre dos y 256 declaraciones externas')
    declarations=tuple(sorted(declarations,key=lambda item:item.inputs.observation_id))
    for values in ((item.inputs.observation_id for item in declarations),(item.inputs.order_id for item in declarations),
                   (item.review_id for item in declarations)):
        if len(set(values))!=len(declarations):raise ProjectionError('El conjunto repite una observación, certificado o revisión')
    baseline=_common_base(declarations)
    if root_account_id not in baseline.accounts:raise ProjectionError('La raíz no pertenece a las carteras revisadas')
    if len(baseline.claims)+len(declarations)>baseline.limits.max_claims:raise ProjectionError('Demasiados recibos conjuntos')
    if (len(baseline.events)+sum(len(item.new_events)for item in declarations)>baseline.limits.max_events
            or sum(len(item.members)for item in declarations)>baseline.limits.max_events):
        raise ProjectionError('Los asientos declarados superan el límite conjunto')
    created=[];claims=[];rows=[];used=set();scales={}
    for item in declarations:
        own=_prepare_created(item.inputs,item.review_id,stamp,item.new_events)
        claim=_claim_for(item.inputs,item.members,own)
        if used.intersection(claim.event_ids)or set(claim.event_ids)&item.inputs.unavailable_event_ids:
            raise ProjectionError('Un asiento no puede asignarse a dos recibos ni reutilizarse')
        used.update(claim.event_ids)
        for scale in item.asset_scales:
            if scale.asset in scales and scales[scale.asset]!=scale.decimals:
                raise ProjectionError('Las declaraciones discrepan sobre la precisión de un activo')
            scales[scale.asset]=scale.decimals
        created.extend(own);claims.append(claim);rows.append((item,own,claim))
    if len({item.id for item in created})!=len(created)or len(baseline.events)+len(created)>baseline.limits.max_events:
        raise ProjectionError('Los originales propuestos se repiten o superan el límite conjunto')
    if {item.order_record_id for item in claims}&{item.order_record_id for item in baseline.claims}:
        raise ProjectionError('Un certificado ya pertenece al criterio anterior')
    all_events=(*baseline.events,*created)
    args=[];priors=[]
    for item,own,claim in rows:
        prior=(*baseline.claims,*(other for other in claims if other.order_record_id!=claim.order_record_id))
        values=_review_args(item.inputs,claim,item.allocations,item.annotations,item.manual_effects,item.asset_scales,tie_orders)
        # Reconcile every receipt before deriving a single asset transfer cost.
        units={key:value for key,value in values.items()if key not in('unavailable_event_ids','tie_orders','limits')}
        validate_external_effect_units(all_events,prior,**units)
        priors.append(prior);args.append(values)
    initial=review_external_effects(all_events,priors[0],**args[0])
    effective={item.original.id:item.accounting_input for item in initial.projection.events}
    normalized=tuple(replace(event,input=replace(event.input,gross_override=effective[event.id].gross_override))
        if event.input.kind in('TRANSFER_IN','TRANSFER_OUT')else event for event in created)
    by_id={event.id:event for event in normalized}
    selection={'schema':SCHEMA,'batch_id':batch_id,'root_account_id':root_account_id,'recorded_at':stamp.isoformat(),
        'declarations':[_selection(item,own,claim)for item,own,claim in rows],
        'tie_orders':[{'account_id':item.account_id,'at':item.at.isoformat(),'nodes':list(item.nodes)}for item in tie_orders]}
    batch_ref=_encoded(selection)[1]
    plans=[];definitive=(*baseline.events,*normalized)
    for index,(item,own,claim)in enumerate(rows):
        review=initial if index==0 and normalized==tuple(created)else review_external_effects(definitive,priors[index],**args[index])
        if plans and (review.body['projection_digest']!=plans[0].review.body['projection_digest']
                or review.body['ledger_input_digest']!=plans[0].review.body['ledger_input_digest']):
            raise ProjectionError('Las revisiones conjuntas no reproducen la misma cronología')
        plans.append(_assembled_plan(item.inputs,item.review_id,stamp,item.new_events,
            tuple(by_id[event.id]for event in own),review,batch_ref=batch_ref))
    body={'schema':SCHEMA,'batch_id':batch_id,'batch_ref':batch_ref,'root_account_id':root_account_id,
        'recorded_at':stamp.isoformat(),'selection':selection,'accounts':list(baseline.accounts),
        'plans':[{'observation_id':item.inputs.observation_id,'digest':plan.digest}for item,plan in zip(declarations,plans,strict=True)],
        'created_event_ids':sorted(by_id),'ledger_input_digest':plans[0].review.body['ledger_input_digest'],
        'projection_digest':plans[0].review.body['projection_digest'],
        'manual_review_complete':all(plan.review.body['manual_review_complete']for plan in plans),
        'saved':False,'acceptance_available':False,'accounting_acceptance_active':False,'moves_funds':False,
        'strategy_executed':False,'resolves_intents':False,'trading_authority':False}
    raw,digest=_encoded(body)
    return ExternalBatchPlan(raw,digest,tuple(plans),tuple(sorted(normalized,key=lambda event:event.id)),plans[0].review.projection)
