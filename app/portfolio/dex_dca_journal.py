"""Causal DCA journal on the existing encrypted DEX tables.

No RPC, signing or sends. A witness records what was settled at each decision;
later receipts cannot rewrite that historical input.
"""
from dataclasses import asdict,replace
from datetime import datetime
from decimal import InvalidOperation
from uuid import UUID,uuid4

from sqlalchemy import select

from app.lab.engine import validate_candles
from app.lab.store import content_hash
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_dca_plan import (ENGINE,DAY,DexDcaCursor,DexDcaLimits,DexDcaAttempt,
    DexDcaExecutionQuote,plan_dca,fold_attempts,validate_execution_quote)
from app.live_execution import dex_dca_plan as _versions
from app.live_execution.dex_permission import DexPermission
from app.live_execution.dex_operator_connection import DexOperatorConnection
from app.live_execution.dex_state import ExecutorState
from app.live_execution.dex_trade import DexTradePlan
from app.portfolio import dex_bot_bindings as bindings,dex_operator_journal as operator
from app.portfolio.dex_controls import _context,_anchor,_validate_anchor,require_settled_policy_controls
from app.portfolio.dex_deployments import deployment_record,_hex_ref
from app.portfolio.models import RealDexBotRun,RealDexBotDecision,RealDexOperatorIntent,RealDexBotBinding
from app.portfolio.wallet_funding import _decoded,_encoded,_time
from app.portfolio.wallet_proof import canonical_wallet_address

OUTCOME_ENGINE=getattr(_versions,'OUTCOME_ENGINE','orbis.dex.bot.dca.v2')
from app.live_execution.dex_versions import (DCA_POLICY_OUTCOME,SUCCESSOR_BINDINGS,RECOVERED_BINDING,RECOVERED_DCA_ROLLOVER,
    DCA_EXTERNAL_OUTCOME,DCA_EXTERNAL_POLICY,EXTERNAL_DCA_ENGINES,EXTERNAL_BINDING,EXTERNAL_DCA_ROLLOVER)
POLICY_OUTCOME_ENGINE=DCA_POLICY_OUTCOME
OUTCOME_ENGINES=(OUTCOME_ENGINE,POLICY_OUTCOME_ENGINE,*EXTERNAL_DCA_ENGINES)
ENGINES=(ENGINE,*OUTCOME_ENGINES)


def _settled(db,row,*,pending_ok=False,at=None,engine=ENGINE):
    options={'include_resolutions':True,'as_of':at} if engine in OUTCOME_ENGINES else {}
    if engine in EXTERNAL_DCA_ENGINES:options['include_external']=True
    return _hub()._settled(db,row.grant_id,pending_ok=pending_ok,run_id=row.id,**options)


def _position(db,row,terms,settled,at,*,engine=ENGINE):
    options={}
    if engine in OUTCOME_ENGINES:
        from app.portfolio.dex_operator_outcomes import outcome_witnesses
        options['resolved_calls']=outcome_witnesses(db,settled,as_of=at,include_external=engine in EXTERNAL_DCA_ENGINES)
        if engine in EXTERNAL_DCA_ENGINES:options['include_external']=True
    return _hub()._position(db,row,terms,settled,at,**options)


def _hub():
    from app.portfolio import dex_bot_journal
    return dex_bot_journal


def _limits(db,binding,grant,window):
    # A policy reset is not fresh lifetime cash or a new per-order mandate.
    if binding['schema'] in SUCCESSOR_BINDINGS:
        root,_,_=bindings.binding_record(db,binding['root_binding_id'])
        _,grant,_=operator.grant_record(db,root.grant_id)
    review=binding['origin']['review']
    result=DexDcaLimits(min(int(review['amounts']['entry_units']),int(grant['limits']['max_buy_per_order'])),
        int(grant['limits']['buy_budget']),int(review['dca']['effective_monthly_units']),window)
    result.validate();return result


def _public(row,terms,state):
    permission=DexPermission.parse(terms['permission'])
    return {'id':row.id,'account_id':row.account_id,'binding_id':row.binding_id,'grant_id':row.grant_id,
        'stored_status':row.status,'expires_at':terms['expires_at'],'head':state['head'],'reason':state['control_reason'],
        'engine':terms['schema'],'permission_mode':permission.mode,'maximum_permission_seconds':permission.requested_seconds,
        **({'previous_run_id':terms['resume']['previous_run_id'],'observation_gap':terms['resume']['observation_gap']} if terms['resume'] else {}),
        'execution_enabled':False,'worker_required':True,'automatic_execution':False}


def _ordered(db,identifier):
    def root_id(binding_id):
        row=db.get(RealDexBotBinding,binding_id)
        if row is None:raise DexExecutorError('Falta la asignación anterior DCA')
        return _decoded(row.payload_json,row.payload_digest).get('root_binding_id',row.id)
    seen=set();result=[];root=None
    while True:
        identifier=str(UUID(identifier))
        if identifier in seen:raise DexExecutorError('El linaje DCA contiene un ciclo')
        seen.add(identifier);row=db.get(RealDexBotRun,identifier)
        if row is None:raise DexExecutorError('Falta un permiso anterior DCA')
        terms=_decoded(row.payload_json,row.payload_digest)
        if root is None:root=root_id(row.binding_id)
        if root_id(row.binding_id)!=root or terms.get('schema') not in ENGINES:raise DexExecutorError('Linaje DCA de otra asignación')
        result.append(row.id)
        if terms['resume'] is None:break
        identifier=terms['resume']['previous_run_id']
    roots=0;successors=set()
    family=[row.id for row in db.scalars(select(RealDexBotBinding)) if root_id(row.id)==root]
    for row in db.scalars(select(RealDexBotRun).where(RealDexBotRun.binding_id.in_(family))):
        terms=_decoded(row.payload_json,row.payload_digest)
        if terms.get('schema') not in ENGINES:raise DexExecutorError('La asignación DCA mezcla motores')
        if terms['resume'] is not None:
            predecessor=db.get(RealDexBotRun,terms['resume']['previous_run_id'])
            if predecessor is not None:
                prior_engine=_decoded(predecessor.payload_json,predecessor.payload_digest)['schema']
                if (prior_engine in EXTERNAL_DCA_ENGINES and terms['schema']not in EXTERNAL_DCA_ENGINES
                        or terms['schema']==ENGINE and prior_engine in OUTCOME_ENGINES
                        or prior_engine==POLICY_OUTCOME_ENGINE and terms['schema']not in(POLICY_OUTCOME_ENGINE,DCA_EXTERNAL_POLICY)):
                    raise DexExecutorError('El linaje DCA no puede retroceder de motor')
        if terms['resume'] is None:roots+=1;continue
        parent=terms['resume']['previous_run_id']
        if parent in successors:raise DexExecutorError('Dos permisos retoman el mismo DCA')
        successors.add(parent)
    if roots!=1:raise DexExecutorError('La asignación DCA debe tener un único origen')
    return list(reversed(result))


def _live_witnesses(db,row,buys,*,engine=ENGINE,at=None):
    result=[]
    for decision,event in buys:
        matches=list(db.scalars(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==row.grant_id,
            RealDexOperatorIntent.decision_ref==decision.payload_digest)))
        if len(matches)>1:raise DexExecutorError('Una activación DCA tiene más de un intento')
        intent=operator.intent_record(db,matches[0].id)[0] if matches else None
        status=intent.status if intent is not None and intent.status in ('FINALIZED','REVERTED','ABANDONED_UNSENT') else 'PENDING'
        witness={'decision_id':decision.id,'intent_id':intent.id if intent else None,'status':status}
        if engine in OUTCOME_ENGINES:
            from app.portfolio.dex_operator_outcomes import operator_outcome
            fact=operator_outcome(db,intent.id,as_of=at,include_external=engine in EXTERNAL_DCA_ENGINES) if intent is not None else None
            witness['status']=('FINALIZED' if fact.outcome=='EXECUTED' else fact.outcome) if fact is not None else 'PENDING'
            witness['resolution']=fact.witness() if fact is not None else None
        result.append(witness)
    return result


def _attempts(db,row,buys,witnesses,settled,anchor,at,*,engine=ENGINE):
    if not isinstance(witnesses,list) or len(witnesses)!=len(buys):raise ValueError('attempt coverage')
    result=[];known=set();hub=_hub()
    for (decision,event),witness in zip(buys,witnesses):
        keys={'decision_id','intent_id','status'}|({'resolution'} if engine in OUTCOME_ENGINES else set())
        if (set(witness)!=keys or witness['decision_id']!=decision.id
                or decision.created_at>at):raise ValueError('attempt witness')
        status=witness['status'];recipe=receipt=None
        if witness['intent_id'] is not None:
            intent,recipe,state=operator.intent_record(db,str(UUID(witness['intent_id'])))
            if (intent.account_id!=row.account_id or intent.grant_id!=row.grant_id or intent.created_at>at
                    or recipe['decision_ref']!=decision.payload_digest or recipe['action']!='BUY'
                    or recipe['trade']['amount']!=event['plan']['amount_units']
                    or any(recipe.get(key)!=value for key,value in event['execution'].items())):raise ValueError('attempt origin')
            fact=None
            if engine in OUTCOME_ENGINES:
                from app.portfolio.dex_operator_outcomes import operator_outcome
                fact=operator_outcome(db,intent.id,as_of=at,include_external=engine in EXTERNAL_DCA_ENGINES)
                effective='FINALIZED' if fact.outcome=='EXECUTED' else fact.outcome
                if witness['resolution']!=fact.witness() or status!=effective:raise ValueError('DCA outcome witness')
            if status in ('FINALIZED','REVERTED') or engine in OUTCOME_ENGINES and status=='CANCELLED' or engine in EXTERNAL_DCA_ENGINES and status=='EXTERNAL_REVIEWED':
                receipt=fact.receipt if fact is not None else state['receipt']
                accepted=fact.accepted_at if fact is not None else intent.updated_at
                effective=status if fact is not None else intent.status
                if (effective!=status or accepted>at or intent.id not in settled
                        or int(receipt['block_number'])>int(anchor['block_number'])
                        or receipt['block_number']==anchor['block_number'] and receipt['block_hash']!=anchor['block_hash']):
                    raise ValueError('receipt witness')
                known.add(intent.id)
            elif status=='ABANDONED_UNSENT':
                if intent.status!=status or intent.updated_at>at or intent.id in settled:raise ValueError('abandon witness')
            elif status=='PENDING':
                if intent.id in settled:raise ValueError('settled pending witness')
            else:raise ValueError('attempt state')
        elif status!='PENDING' or engine in OUTCOME_ENGINES and witness['resolution'] is not None:raise ValueError('missing attempt')
        dca=event['plan']['dca']
        result.append(DexDcaAttempt(decision.id,dca['key'],dca['kind'],event['plan']['candle_t'],decision.created_at,
            dca['reference'],dca.get('drop_pct'),int(event['plan']['amount_units']),status,
            _time(datetime.fromisoformat(receipt['block_at'])) if receipt else None,int(receipt['gas_fee_wei']) if receipt else 0))
    if known!=set(settled):raise ValueError('unattributed settlement')
    # Also verify physical counters/nonce in the chosen receipt sequence.
    if engine in OUTCOME_ENGINES:
        from app.portfolio.dex_operator_outcomes import outcome_witnesses
        references=sorted([item['resolution'] for item in witnesses if item['resolution'] is not None],key=lambda item:item['intent_id'])
        if references!=outcome_witnesses(db,settled,as_of=at,include_external=engine in EXTERNAL_DCA_ENGINES):raise ValueError('DCA resolution coverage')
    _position(db,row,_decoded(row.payload_json,row.payload_digest),settled,at,engine=engine)
    return tuple(result)


def _planning(terms,review,bars,cursor,snapshot,quote,attempts,at):
    # CONFIGURE resets last_entry_at on chain. The previous verified fill's
    # cooldown remains in force; stored observations/recipes stay physical.
    last=max([snapshot.last_entry_at,*[int(item.executed_at.timestamp()) for item in attempts if item.status=='FINALIZED']])
    snapshot=replace(snapshot,last_entry_at=last)
    return dict(definition=review['definition'],candles=bars,cursor=cursor,snapshot=snapshot,quote=quote,
        limits=DexDcaLimits(**terms['dca']['limits']),attempts=attempts,skip_through_t=terms['dca']['skip_through_t'],now=at,
        **({'engine':terms['schema']} if terms['schema'] in OUTCOME_ENGINES else {}))


def _check_execution(recipe,plan,terms,grant,observation,planning,at):
    if plan.action!='BUY':
        if recipe is not None:raise ValueError('unexpected DCA execution')
        return
    if not isinstance(recipe,dict):raise ValueError('missing DCA execution')
    operator._validate_recipe(recipe,grant,at)
    keys={'action','wallet_digest','owner_digest','creation_nonce','policy_id','contract_nonce','nonce','gas','gas_price',
        'max_gas_wei','operator_balance','anchor','trade'}
    if (set(recipe)!=keys or recipe['action']!='BUY' or recipe['creation_nonce']!=terms['creation_nonce']
            or recipe['anchor']!=observation['anchor'] or recipe['trade']['amount']!=str(plan.amount_units)):
        raise ValueError('DCA executable scope')
    quote=DexDcaExecutionQuote(True,int(recipe['trade']['amount']),int(recipe['trade']['quote_out']),
        int(recipe['trade']['deadline']),datetime.fromisoformat(recipe['anchor']['observed_at']))
    validate_execution_quote(quote,**planning)


def _replay_event(db,row,terms,grant,review,bars,cursor,old_settled,buys,carried,event):
    hub=_hub();keys={'schema','run_id','sequence_no','previous_digest','claim_ref','at','candle','quote','snapshot',
        'settled_ids','attempts','exit_cap_units','plan','execution','cursor'}
    if (set(event)!=keys or event['schema']!=terms['schema'] or event['run_id']!=row.id or event['claim_ref']!=terms['claim_ref']
            or event['exit_cap_units']!='0'):raise ValueError('DCA event scope')
    at=_time(datetime.fromisoformat(event['at']))
    if not row.created_at<=at<datetime.fromisoformat(terms['expires_at']) or at<cursor.last_checked_at:raise ValueError('DCA event clock')
    if event['candle'] is not None:
        if event['candle']['t']!=bars[-1].t+DAY:raise ValueError('DCA new candle')
        added,gaps=validate_candles([bar.json() for bar in bars[-2:]]+[event['candle']],'1d',now=int(at.timestamp()))
        if gaps or event['candle']!=added[-1].json():raise ValueError('DCA candle coverage')
        bars=[*bars,added[-1]]
    settled=event['settled_ids'];observed=event['snapshot']
    if not set(old_settled)<=set(settled):raise ValueError('DCA lost settlement')
    position,_=_position(db,row,terms,settled,at,engine=terms['schema'])
    if position!={key:int(value) for key,value in observed['anchor']['counters'].items()}:raise ValueError('DCA physical lineage')
    snapshot=hub._decision_state(observed,grant,terms['creation_nonce'],at)
    attempts=_attempts(db,row,buys,event['attempts'],settled,observed['anchor'],at,engine=terms['schema'])
    planning=_planning(terms,review,bars,cursor,snapshot,hub._quote(event['quote']),(*carried,*attempts),at)
    plan=plan_dca(**planning)
    if event['plan']!=plan.json():raise ValueError('DCA decision replay')
    _check_execution(event['execution'],plan,terms,grant,observed,planning,at)
    following=DexDcaCursor(bars[-1].t,at,plan.reference)
    if event['cursor']!=hub._cursor_json(following):raise ValueError('DCA cursor replay')
    return bars,following,settled


def run_record(db,identifier,*,verify_full=False):
    from app.portfolio.dex_verified_read import read_record
    def verify(current):
        parents={}
        for previous in _ordered(db,identifier):
            key=('run',previous);value=current['values'].get(key)
            if value is None:value=_run_record(db,previous,parents=parents);current['values'][key]=value
            parents[previous]=value
        return parents[str(UUID(identifier))]
    return read_record(db,'run',identifier,verify,force_full=verify_full)


def _facts_digest(attempts):
    return content_hash([{**asdict(item),'decided_at':item.decided_at.isoformat(),
        'executed_at':item.executed_at.isoformat() if item.executed_at else None,
        'amount_units':str(item.amount_units),'gas_fee_wei':str(item.gas_fee_wei)} for item in attempts])


def _closed_attempts(db,previous,*,anchor,at,engine=ENGINE):
    hub=_hub();row,terms,state,archive=previous
    if row.status!='STOPPED' or at<=row.updated_at:raise DexExecutorError('Detén y concilia el permiso DCA anterior')
    settled=_settled(db,row,at=at,engine=engine)
    witnesses=_live_witnesses(db,row,archive['buy_decisions'],engine=engine,at=at)
    own=_attempts(db,row,archive['buy_decisions'],witnesses,settled,anchor,at,engine=engine)
    # A stopped epoch cannot reserve its old decision: the emission kernel
    # requires ACTIVE and the latest head. No outbox is proof of never sent,
    # only after this explicit stopped-predecessor barrier.
    own=tuple(replace(item,status='ABANDONED_UNSENT') if witness['intent_id'] is None else item
        for item,witness in zip(own,witnesses))
    if any(item.status=='PENDING' for item in own):raise DexExecutorError('Resuelve el intento DCA anterior antes de rearmar')
    carried=(*archive['carried_attempts'],*own)
    fold_attempts(carried,definition=archive['review']['definition'],now=max(at,datetime.fromisoformat(anchor['block_at'])),
        **({'engine':engine} if engine in OUTCOME_ENGINES else {}))
    return settled,carried


def _resume_material(db,previous,*,anchor,seed,at,child_id,binding,grant,engine=ENGINE):
    hub=_hub();row,terms,state,archive=previous
    if row.status!='STOPPED' or at<=row.updated_at:raise DexExecutorError('Detén y concilia el permiso DCA anterior')
    old=[bar.json() for bar in archive['bars']]
    if len(seed)<len(old) or seed[:len(old)]!=old:raise DexExecutorError('La recuperación DCA perdió el histórico causal')
    if row.binding_id!=binding['id']:
        if engine in(OUTCOME_ENGINE,DCA_EXTERNAL_OUTCOME):
            raise DexExecutorError('El formato DCA v2 no admite renovar con resultados recuperados')
        return _resume_policy(db,previous,anchor=anchor,seed=seed,at=at,child_id=child_id,binding=binding,grant=grant,engine=engine)
    later=db.scalar(select(RealDexBotRun.id).where(RealDexBotRun.binding_id==row.binding_id,
        RealDexBotRun.id.not_in((row.id,child_id)),RealDexBotRun.created_at>=row.created_at,RealDexBotRun.created_at<=at).limit(1))
    if later is not None:raise DexExecutorError('Rearma el último permiso DCA, no uno anterior')
    settled,carried=_closed_attempts(db,previous,anchor=anchor,at=at,engine=engine)
    position,_=_position(db,row,terms,settled,at,engine=engine)
    if (position!={key:int(value) for key,value in anchor['counters'].items()}
            or anchor['operator_ref']!=terms['baseline']['operator_ref']):raise DexExecutorError('La posición DCA cambió fuera de sus recibos')
    cursor=DexDcaCursor(seed[-1]['t'],at,archive['cursor'].reference)
    skip=terms['dca']['skip_through_t'] if seed[-1]['t']==archive['cursor'].last_candle_t else seed[-1]['t']
    material={'previous_run_id':row.id,'previous_payload_digest':row.payload_digest,'previous_state_digest':row.state_digest,
        'settled_ids':settled,'facts_digest':_facts_digest(carried),'initial_cursor':hub._cursor_json(cursor),
        'skip_through_t':skip,'observation_gap':{'from':archive['cursor'].last_checked_at.isoformat(),'to':at.isoformat()}}
    if engine in OUTCOME_ENGINES:
        from app.portfolio.dex_operator_outcomes import outcome_witnesses
        material['resolved_calls']=outcome_witnesses(db,settled,as_of=at,include_external=engine in EXTERNAL_DCA_ENGINES)
    return material,cursor,carried


def _resume_policy(db,previous,*,anchor,seed,at,child_id,binding,grant,engine=ENGINE):
    from app.portfolio.dex_controls import control_record
    row,terms,state,archive=previous
    external=engine==DCA_EXTERNAL_POLICY
    expected_binding=EXTERNAL_BINDING if external else RECOVERED_BINDING if engine==POLICY_OUTCOME_ENGINE else 'orbis.dex.bot.binding.v2'
    if binding['schema']!=expected_binding:raise DexExecutorError('Falta la renovación DCA confirmada de este formato')
    _,control,_=control_record(db,binding['rollover_id']);proof=control['bot_rollover']
    expected_proof=EXTERNAL_DCA_ROLLOVER if external else RECOVERED_DCA_ROLLOVER if engine==POLICY_OUTCOME_ENGINE else 'orbis.dex.bot.dca.policy-rollover.v1'
    recovered=engine in(POLICY_OUTCOME_ENGINE,DCA_EXTERNAL_POLICY)
    if (proof['schema']!=expected_proof or row.id!=proof['previous_run_id']
            or row.binding_id!=proof['previous_binding_id'] or row.payload_digest!=proof['previous_payload_digest']
            or row.state_digest!=proof['previous_state_digest'] or anchor['counters']!=grant['anchor']['counters']
            or anchor['operator_ref']!=grant['anchor']['operator_ref']
            or db.scalar(select(RealDexBotRun.id).where(RealDexBotRun.binding_id==binding['id'],
                RealDexBotRun.id!=child_id,RealDexBotRun.created_at<=at).limit(1)) is not None
            or _hub()._settled(db,row.grant_id,**({'include_resolutions':True,'as_of':at,'include_external':external} if recovered else {}))!=proof['settled_ids']):
        raise DexExecutorError('La continuidad DCA no corresponde al cierre y recibo de la política anterior')
    settled,carried=_closed_attempts(db,previous,anchor=control['anchor'],at=at,engine=engine)
    if _facts_digest(carried)!=proof['dca']['facts_digest']:raise DexExecutorError('Las activaciones DCA cambiaron tras la renovación')
    cursor=DexDcaCursor(seed[-1]['t'],at,archive['cursor'].reference)
    skip=terms['dca']['skip_through_t'] if seed[-1]['t']==archive['cursor'].last_candle_t else seed[-1]['t']
    material={'previous_run_id':row.id,'previous_payload_digest':row.payload_digest,'previous_state_digest':row.state_digest,
        'settled_ids':settled,'facts_digest':_facts_digest(carried),'initial_cursor':_hub()._cursor_json(cursor),
        'skip_through_t':skip,'observation_gap':{'from':archive['cursor'].last_checked_at.isoformat(),'to':at.isoformat()}}
    if recovered:
        from app.portfolio.dex_operator_outcomes import outcome_witnesses
        if outcome_witnesses(db,proof['settled_ids'],as_of=at,include_external=external)!=proof['resolved_calls']:
            raise DexExecutorError('Las resoluciones DCA cambiaron desde la renovación')
        material['resolved_calls']=outcome_witnesses(db,settled,as_of=at,include_external=external)
    return material,cursor,carried


def _run_record(db,identifier,*,parents):
    hub=_hub();hub._schema(db);row=db.get(RealDexBotRun,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Sesión DCA DEX no encontrada')
    terms,state=_decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
    try:
        keys={'schema','id','account_id','binding_id','grant_id','wallet_digest','claim_ref','connection_request_id',
            'expires_at','permission','seed','baseline','creation_nonce','source_hash','dca','resume'}
        if (set(terms) not in (keys,keys|{'request_digest'}) or terms['schema'] not in ENGINES or terms['id']!=row.id
                or terms['account_id']!=row.account_id or terms['binding_id']!=row.binding_id or terms['grant_id']!=row.grant_id
                or not _hex_ref(terms['claim_ref']) or str(UUID(terms['connection_request_id']))!=terms['connection_request_id']
                or 'request_digest' in terms and not _hex_ref(terms['request_digest'])):raise ValueError('DCA run identity')
        if terms['schema']in EXTERNAL_DCA_ENGINES and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<78:
            raise ValueError('external DCA outcome schema')
        parent,binding,_=bindings.binding_record(db,row.binding_id);_,grant,_=operator.grant_record(db,row.grant_id)
        review=binding['origin']['review'];permission=DexPermission.parse(terms['permission'])
        if 'volume_gate' in review['definition']:
            from app.portfolio.dex_research import authorization_qualification
            if authorization_qualification(db,row.id) is None:raise ValueError('DCA volume qualification')
        if (binding['schema']==RECOVERED_BINDING and terms['schema']not in(POLICY_OUTCOME_ENGINE,DCA_EXTERNAL_POLICY)
                or binding['schema']==EXTERNAL_BINDING and terms['schema']!=DCA_EXTERNAL_POLICY
                or terms['schema']==POLICY_OUTCOME_ENGINE and binding['schema']!=RECOVERED_BINDING
                or terms['schema']==DCA_EXTERNAL_POLICY and binding['schema']not in SUCCESSOR_BINDINGS):raise ValueError('DCA recovered policy scope')
        expiry=_time(datetime.fromisoformat(terms['expires_at']))
        if (parent.account_id!=row.account_id or parent.grant_id!=row.grant_id or terms['wallet_digest']!=binding['wallet_digest']
                or review['source_kind']!='DCA' or review['source']['timeframe']!='1d'
                or grant['limits']['allow_sell'] is not False or parent.created_at>row.created_at
                or terms['creation_nonce']!=deployment_record(db,parent.deployment_id)[1]['nonce']
                or terms['source_hash']!=content_hash(review['source'])
                or not row.created_at<expiry<=datetime.fromtimestamp(grant['limits']['expires_at'],row.created_at.tzinfo)
                or (expiry-row.created_at).total_seconds()>permission.requested_seconds):raise ValueError('DCA source/permission')
        bars,gaps=validate_candles(terms['seed'],'1d',now=int(row.created_at.timestamp()))
        seed=binding['origin']['seed'];config=terms['dca']
        if (gaps or terms['seed']!=[bar.json() for bar in bars] or terms['seed'][:len(seed)]!=seed or len(bars)<len(seed)
                or bars[-1].t!=int(row.created_at.timestamp())//DAY*DAY-DAY
                or set(config)!={'limits','initial_reference','skip_through_t'}
                or config['limits']!=asdict(_limits(db,binding,grant,config['limits']['activation_window_seconds']))):raise ValueError('DCA initial configuration')
        _validate_anchor(terms['baseline'],terms['creation_nonce'],row.created_at)
        carried=()
        if terms['resume'] is None:
            if terms['schema']!=ENGINE:raise ValueError('DCA outcome engine requires continuation')
            if (binding['schema']!='orbis.dex.bot.binding.v1' or terms['baseline']['counters']!=grant['anchor']['counters'] or terms['baseline']['operator_ref']!=grant['anchor']['operator_ref']
                    or config['initial_reference']!=str(bars[-1].c) or config['skip_through_t']!=bars[-1].t):raise ValueError('DCA initial position')
            cursor=DexDcaCursor(bars[-1].t,row.created_at,config['initial_reference'])
        else:
            previous=parents[terms['resume']['previous_run_id']]
            if previous[0].account_id!=row.account_id:raise ValueError('DCA predecessor scope')
            material,cursor,carried=_resume_material(db,previous,anchor=terms['baseline'],seed=terms['seed'],at=row.created_at,child_id=row.id,binding=binding,grant=grant,engine=terms['schema'])
            if (material!=terms['resume'] or config['initial_reference']!=previous[1]['dca']['initial_reference']
                    or config['limits']!=previous[1]['dca']['limits'] or config['skip_through_t']!=material['skip_through_t']):raise ValueError('DCA resume material')
        previous=row.payload_digest;sequence=-1;settled=[];buys=[];last_plan=None;last_average=None;last_volume=None
        for item in db.scalars(select(RealDexBotDecision).where(RealDexBotDecision.run_id==row.id).order_by(RealDexBotDecision.sequence_no)):
            event=_decoded(item.payload_json,item.payload_digest)
            if (str(UUID(item.id))!=item.id or type(event['sequence_no']) is not int or item.sequence_no!=sequence+1
                    or event['sequence_no']!=item.sequence_no or item.previous_digest!=previous
                    or event['previous_digest']!=previous or event['at']!=item.created_at.isoformat()):raise ValueError('DCA decision chain')
            bars,cursor,settled=_replay_event(db,row,terms,grant,review,bars,cursor,settled,buys,carried,event)
            if event['plan']['action']=='BUY':buys.append((item,event))
            last_plan=event['plan'];sequence=item.sequence_no;previous=item.payload_digest
            if 'average' in last_plan['evidence']:
                last_average={'candle_t':last_plan['candle_t'],'reason':last_plan['reason'],
                    'signal':last_plan['evidence']['average']}
            if 'volume' in last_plan['evidence']:
                last_volume={'candle_t':last_plan['candle_t'],'reason':last_plan['reason'],
                    'signal':last_plan['evidence']['volume']}
        if (set(state)!={'status','history','head','control_reason'} or row.status not in ('ACTIVE','PAUSED','STOPPED')
                or state['status']!=row.status or type(state['head']['sequence_no']) is not int
                or state['head']!={'sequence_no':sequence,'digest':previous,'cursor':hub._cursor_json(cursor)}):raise ValueError('DCA head')
        history=state['history'];statuses=[event['status'] for event in history]
        stamps=[_time(datetime.fromisoformat(event['at'])) for event in history]
        if (not 1<=len(history)<=3 or any(set(event)!={'status','at'} for event in history) or statuses[0]!='ACTIVE'
                or statuses[-1]!=row.status or len(set(statuses))!=len(statuses) or stamps!=sorted(stamps) or stamps[0]!=row.created_at
                or any(stamp.isoformat()!=event['at'] for stamp,event in zip(stamps,history))
                or row.updated_at!=max(cursor.last_checked_at,stamps[-1])
                or any(right not in {'ACTIVE':{'PAUSED','STOPPED'},'PAUSED':{'STOPPED'}}.get(left,set()) for left,right in zip(statuses,statuses[1:]))
                or row.status!='ACTIVE' and cursor.last_checked_at>stamps[-1]):raise ValueError('DCA run history')
        if ((last_plan is not None and last_plan['action']=='PAUSE')!=('PAUSED' in statuses)
                or row.status=='ACTIVE' and state['control_reason'] is not None
                or row.status=='PAUSED' and state['control_reason']!=last_plan['reason']
                or row.status=='STOPPED' and state['control_reason'] not in hub.CONTROL_REASONS):raise ValueError('DCA control reason')
        # The immutable v1 decision grammar stays unchanged. This current read
        # projection may include independently accepted outcomes after it stopped.
        live_engine=terms['schema']
        if live_engine not in EXTERNAL_DCA_ENGINES and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78:
            candidates=_live_witnesses(db,row,buys,engine=DCA_EXTERNAL_OUTCOME)
            if any(item['resolution']is not None and 'external_review'in item['resolution']for item in candidates):
                live_engine=DCA_EXTERNAL_OUTCOME
        if live_engine==ENGINE and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
            candidates=_live_witnesses(db,row,buys,engine=OUTCOME_ENGINE)
            if any(item['resolution'] is not None for item in candidates):live_engine=OUTCOME_ENGINE
        witnesses=_live_witnesses(db,row,buys,engine=live_engine)
        finals=_settled(db,row,pending_ok=True,engine=live_engine)
        horizon=row.updated_at;block=terms['baseline']['block_number'];block_hash=terms['baseline']['block_hash']
        for witness in witnesses:
            if witness['intent_id'] is None:continue
            intent,_,proof=operator.intent_record(db,witness['intent_id']);horizon=max(horizon,intent.updated_at)
            receipt=proof['receipt']
            if live_engine in OUTCOME_ENGINES:
                from app.portfolio.dex_operator_outcomes import operator_outcome
                fact=operator_outcome(db,intent.id,include_external=live_engine in EXTERNAL_DCA_ENGINES);receipt=fact.receipt
                if fact.accepted_at is not None:horizon=max(horizon,fact.accepted_at)
            if receipt is not None:
                horizon=max(horizon,datetime.fromisoformat(receipt['block_at']))
                if int(receipt['block_number'])>=int(block):block,block_hash=receipt['block_number'],receipt['block_hash']
        attempts=_attempts(db,row,buys,witnesses,finals,{'block_number':block,'block_hash':block_hash},horizon,engine=live_engine)
        # A rearm can carry a receipt a few seconds ahead of local time. Its
        # verified baseline already accepts that chain skew. Keep decision time
        # unchanged and use the same bounded chain horizon when folding facts.
        chain_horizon=max(horizon,datetime.fromisoformat(terms['baseline']['block_at']))
        projection=fold_attempts((*carried,*attempts),definition=review['definition'],now=horizon,observed_chain_at=chain_horizon,
            **({'engine':live_engine} if live_engine in OUTCOME_ENGINES else {}))
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,OverflowError,InvalidOperation):
        raise DexExecutorError('El archivo causal DCA DEX no es coherente') from None
    return row,terms,state,{'bars':bars,'cursor':cursor,'settled':settled,'review':review,'grant':grant,
        'dca_projection':projection,'buy_decisions':buys,'carried_attempts':carried,
        'last_average':last_average,'last_volume':last_volume}


def start_run(db,*,identifier,account_id,binding_id,wallet_address,connection,snapshot,seed,now,
              permission_mode=None,requested_seconds=None,request_digest=None,previous_run_id=None,activation_window_seconds=900,research_check=None):
    hub=_hub();now=_time(now);identifier,account_id,binding_id=[str(UUID(value)) for value in (identifier,account_id,binding_id)]
    wallet=canonical_wallet_address(wallet_address);permission=DexPermission(permission_mode,requested_seconds);permission.validate()
    if previous_run_id is not None:previous_run_id=str(UUID(previous_run_id))
    if request_digest is not None and not _hex_ref(request_digest):raise DexExecutorError('Huella DCA no válida')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');hub._schema(db)
    if db.get(RealDexBotRun,identifier) is not None:
        row,terms,state,_=run_record(db,identifier)
        if (row.account_id!=account_id or row.binding_id!=binding_id or terms['wallet_digest']!=operator._identity(db,'wallet',wallet)
                or terms.get('request_digest')!=request_digest or terms['permission']!=permission.json()
                or terms['dca']['limits']['activation_window_seconds']!=activation_window_seconds
                or (terms['resume']['previous_run_id'] if terms['resume'] else None)!=previous_run_id):raise DexExecutorError('ID de permiso DCA ya usado')
        return _public(row,terms,state),None
    if type(connection) is not DexOperatorConnection:raise DexExecutorError('Conecta la operadora verificada')
    live=connection.status(account_id=account_id,wallet_address=wallet,now=now)
    parent,binding,_=bindings.binding_record(db,binding_id);grant_row,grant,_=operator.grant_record(db,parent.grant_id)
    if 'volume_gate' in binding['origin']['review']['definition'] and research_check is None:
        raise DexExecutorError('Verifica la reserva antes de activar o rearmar el DCA de volumen')
    require_settled_policy_controls(db,parent.deployment_id)
    if (not live['connected'] or live['binding_id']!=binding_id or live['grant_id']!=parent.grant_id or parent.account_id!=account_id
            or parent.status!='READY' or grant_row.status!='ACTIVE' or binding['wallet_digest']!=operator._identity(db,'wallet',wallet)
            or binding['origin']['review']['source_kind']!='DCA'
            or now<parent.created_at):raise DexExecutorError('Vínculo DCA no preparado')
    _context(db,parent.deployment_id,account_id,wallet,snapshot,now)
    if (snapshot.finalized is not True or snapshot.policy_limits is None or snapshot.policy_limits.as_dict()!=grant['limits']
            or operator._identity(db,'wallet',snapshot.operator)!=grant['operator_digest']
            or previous_run_id is None and _anchor(db,snapshot)['counters']!=grant['anchor']['counters']):
        raise DexExecutorError('La primera activación DCA no adopta otro permiso o posición')
    resolved_support=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71
    external_support=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78
    settled=hub._settled(db,parent.grant_id,**({'include_resolutions':True,'as_of':now,'include_external':external_support} if previous_run_id is not None and resolved_support else {}))
    if previous_run_id is None and (binding['schema']!='orbis.dex.bot.binding.v1' or settled or db.scalar(select(RealDexBotRun.id).where(RealDexBotRun.binding_id==binding_id).limit(1)) is not None):
        raise DexExecutorError('Retoma el último permiso DCA para conservar sus activaciones')
    bars,gaps=validate_candles(seed,'1d',now=int(now.timestamp()));origin=binding['origin']['seed']
    if (gaps or len(seed)<len(origin) or seed[:len(origin)]!=origin or seed!=[bar.json() for bar in bars]
            or bars[-1].t!=int(now.timestamp())//DAY*DAY-DAY):raise DexExecutorError('Histórico DCA inicial sin continuidad')
    expiry=datetime.fromisoformat(live['expires_at'])
    if not now<expiry or (expiry-now).total_seconds()>permission.requested_seconds:raise DexExecutorError('Vigencia DCA no válida')
    claim=str(uuid4());config={'initial_reference':str(bars[-1].c),'skip_through_t':bars[-1].t,
        'limits':asdict(_limits(db,binding,grant,activation_window_seconds))}
    resume=None;cursor=DexDcaCursor(bars[-1].t,now,config['initial_reference']);engine=ENGINE
    if previous_run_id is not None:
        previous=run_record(db,previous_run_id)
        if (previous[0].account_id!=account_id
                or previous[1]['dca']['limits']!=config['limits']):raise DexExecutorError('El permiso anterior pertenece a otro DCA o límites')
        if resolved_support:
            from app.portfolio.dex_operator_outcomes import outcome_witnesses
            references=outcome_witnesses(db,settled,as_of=now,include_external=external_support)
            use_external=binding['schema']==EXTERNAL_BINDING or previous[1]['schema']in EXTERNAL_DCA_ENGINES or any('external_review'in item for item in references)
            if use_external:
                engine=DCA_EXTERNAL_POLICY if binding['schema']in SUCCESSOR_BINDINGS else DCA_EXTERNAL_OUTCOME
            elif binding['schema']==RECOVERED_BINDING:engine=POLICY_OUTCOME_ENGINE
            elif previous[1]['schema'] in OUTCOME_ENGINES or references:engine=OUTCOME_ENGINE
        resume,cursor,_=_resume_material(db,previous,anchor=_anchor(db,snapshot),seed=seed,at=now,child_id=identifier,binding=binding,grant=grant,engine=engine)
        config['initial_reference']=previous[1]['dca']['initial_reference'];config['skip_through_t']=resume['skip_through_t']
    terms={'schema':engine,'id':identifier,'account_id':account_id,'binding_id':binding_id,'grant_id':parent.grant_id,
        'wallet_digest':binding['wallet_digest'],'claim_ref':operator._identity(db,'bot-claim',claim),'connection_request_id':live['request_id'],
        'expires_at':live['expires_at'],'permission':permission.json(),'seed':seed,'baseline':_anchor(db,snapshot),
        'creation_nonce':deployment_record(db,parent.deployment_id)[1]['nonce'],'source_hash':content_hash(binding['origin']['review']['source']),'dca':config,'resume':resume}
    if request_digest is not None:terms['request_digest']=request_digest
    raw,digest=_encoded(terms)
    state={'status':'ACTIVE','control_reason':None,'history':[{'status':'ACTIVE','at':now.isoformat()}],
        'head':{'sequence_no':-1,'digest':digest,'cursor':hub._cursor_json(cursor)}}
    state_raw,state_digest=_encoded(state);row=RealDexBotRun(id=identifier,account_id=account_id,binding_id=binding_id,grant_id=parent.grant_id,
        payload_json=raw,payload_digest=digest,state_json=state_raw,state_digest=state_digest,status='ACTIVE',created_at=now,updated_at=now)
    db.add(row);db.flush()
    if research_check is not None:
        from app.portfolio.dex_research import save_authorization_qualification
        save_authorization_qualification(db,identifier,research_check)
    run_record(db,identifier);db.commit();return _public(row,terms,state),claim


def _decision_context(db,identifier,*,account_id,claim,candles,quote,snapshot,exit_cap_units,now,expected_head=None):
    hub=_hub();now=_time(now);row,terms,state,archive=run_record(db,identifier)
    if (type(snapshot) is not ExecutorState or exit_cap_units!=0 or row.account_id!=account_id or row.status!='ACTIVE'
            or operator._identity(db,'bot-claim',str(UUID(claim)))!=terms['claim_ref'] or now>=datetime.fromisoformat(terms['expires_at'])
            or expected_head is not None and state['head']['digest']!=expected_head):raise DexExecutorError('El permiso o revisión DCA cambió')
    parent,binding,_=bindings.binding_record(db,row.binding_id);grant_row,grant,_=operator.grant_record(db,row.grant_id)
    require_settled_policy_controls(db,parent.deployment_id);_context(db,parent.deployment_id,account_id,snapshot.owner,snapshot,now)
    if (parent.status!='READY' or grant_row.status!='ACTIVE' or snapshot.policy_limits is None or snapshot.policy_limits.as_dict()!=grant['limits']
            or operator._identity(db,'wallet',snapshot.owner)!=terms['wallet_digest']
            or operator._identity(db,'wallet',snapshot.operator)!=grant['operator_digest']):raise DexExecutorError('Autoridad DCA distinta')
    prior=[bar.json() for bar in archive['bars']]
    if (not isinstance(candles,list) or len(candles) not in (len(prior),len(prior)+1) or candles[:len(prior)]!=prior):
        raise DexExecutorError('Falta continuidad de velas DCA')
    bars,gaps=validate_candles(candles,'1d',now=int(now.timestamp()))
    if gaps or candles!=[bar.json() for bar in bars]:raise DexExecutorError('Velas DCA no válidas')
    settled=_settled(db,row,pending_ok=True,at=now,engine=terms['schema'])
    position,_=_position(db,row,terms,settled,now,engine=terms['schema'])
    anchor=_anchor(db,snapshot)
    if position!={key:int(value) for key,value in anchor['counters'].items()}:raise DexExecutorError('La posición DCA no coincide con sus recibos')
    witnesses=_live_witnesses(db,row,archive['buy_decisions'],engine=terms['schema'],at=now)
    attempts=_attempts(db,row,archive['buy_decisions'],witnesses,settled,anchor,now,engine=terms['schema'])
    planning=_planning(terms,archive['review'],bars,archive['cursor'],snapshot,quote,(*archive['carried_attempts'],*attempts),now)
    return row,terms,state,archive,settled,bars,witnesses,planning,plan_dca(**planning)


def preview_decision(db,identifier,**arguments):return _decision_context(db,identifier,**arguments)[-1]


def record_decision(db,identifier,*,execution,**arguments):
    hub=_hub();now=_time(arguments['now']);db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    row,terms,state,archive,settled,bars,witnesses,planning,plan=_decision_context(db,identifier,**arguments)
    recipe=None
    if plan.action=='BUY':
        if type(execution) is not DexTradePlan or execution.snapshot!=arguments['snapshot']:raise DexExecutorError('Simula la compra DCA exacta')
        execution.validate(now);recipe=operator._recipe(db,execution)
    elif execution is not None:raise DexExecutorError('La decisión DCA no permite operar')
    _check_execution(recipe,plan,terms,archive['grant'],hub._state_json(db,arguments['snapshot']),planning,now)
    sequence=state['head']['sequence_no']+1
    event={'schema':terms['schema'],'run_id':row.id,'sequence_no':sequence,'previous_digest':state['head']['digest'],'claim_ref':terms['claim_ref'],
        'at':now.isoformat(),'candle':arguments['candles'][-1] if len(bars)>len(archive['bars']) else None,
        'quote':arguments['quote'].json(),'snapshot':hub._state_json(db,arguments['snapshot']),'settled_ids':settled,
        'attempts':witnesses,'exit_cap_units':'0','plan':plan.json(),'execution':recipe,
        'cursor':hub._cursor_json(DexDcaCursor(bars[-1].t,now,plan.reference))}
    _replay_event(db,row,terms,archive['grant'],archive['review'],archive['bars'],archive['cursor'],archive['settled'],archive['buy_decisions'],archive['carried_attempts'],event)
    raw,digest=_encoded(event);db.add(RealDexBotDecision(id=str(uuid4()),run_id=row.id,sequence_no=sequence,previous_digest=state['head']['digest'],
        payload_json=raw,payload_digest=digest,created_at=now))
    state['head']={'sequence_no':sequence,'digest':digest,'cursor':event['cursor']}
    if plan.action=='PAUSE':
        row.status=state['status']='PAUSED';state['control_reason']=plan.reason;state['history'].append({'status':'PAUSED','at':now.isoformat()})
    row.updated_at=now;row.state_json,row.state_digest=_encoded(state);db.flush();run_record(db,row.id);db.commit()
    return {'run_id':row.id,'decision_ref':digest,'plan':plan.json(),'sequence_no':sequence}
