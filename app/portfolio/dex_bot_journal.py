"""Causal LONG journal: closed bars, actual receipts and exact executor recipes.

No network, signing or worker is hidden in these SQL operations. A stored ACTIVE
permission alone is never presented as a running bot. Reopen cannot start it.
"""
from dataclasses import asdict,replace
from datetime import datetime
from decimal import Decimal,InvalidOperation
from uuid import UUID,uuid4

from sqlalchemy import select

from app.lab.engine import INTERVALS,validate_candles
from app.lab.store import content_hash
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_plan import DexBotCursor,DexReferenceQuote,DexDecisionState,plan_strategy
from app.live_execution.dex_dca_plan import ENGINE as DCA_ENGINE
from app.live_execution.dex_permission import DexPermission,LEGACY_PERMISSION_SECONDS,LONG_ENGINE_V1,LONG_ENGINE_V2,LONG_ENGINE_V3,LONG_ENGINE_V4,LONG_ENGINE_V5
from app.live_execution.dex_operator_connection import DexOperatorConnection
from app.live_execution.dex_policy import PolicyLimits
from app.live_execution.dex_state import ExecutorState
from app.live_execution.dex_trade import DexTradePlan
from app.portfolio import dex_bot_bindings as bindings,dex_operator_journal as operator
from app.portfolio.dex_controls import _context,_anchor,_validate_anchor,require_settled_policy_controls
from app.portfolio.dex_deployments import deployment_record,_hex_ref
from app.portfolio.models import RealDexBotRun,RealDexBotDecision,RealDexOperatorIntent,RealDexBotBinding
from app.portfolio.wallet_funding import _encoded,_decoded,_time,_uint
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.dex_bot_replay_cache import VerifiedPrefix,witness,receipt_witnesses,remember

from app.live_execution.dex_versions import (DCA_POLICY_OUTCOME,LONG_POLICY_OUTCOME,RECOVERED_BINDING,SUCCESSOR_BINDINGS,
    LONG_EXTERNAL_OUTCOME,LONG_EXTERNAL_POLICY,EXTERNAL_BINDING,EXTERNAL_LONG_ENGINES,EXTERNAL_DCA_ENGINES)
DCA_ENGINES=(DCA_ENGINE,'orbis.dex.bot.dca.v2',DCA_POLICY_OUTCOME,*EXTERNAL_DCA_ENGINES)

ENGINE=LONG_ENGINE_V1
EXTENDED_ENGINE=LONG_ENGINE_V2
CONTINUATION_ENGINE=LONG_ENGINE_V3
POLICY_ENGINE=LONG_ENGINE_V4
OUTCOME_ENGINE=LONG_ENGINE_V5
POLICY_OUTCOME_ENGINE=LONG_POLICY_OUTCOME
POLICY_ENGINES=(POLICY_ENGINE,POLICY_OUTCOME_ENGINE,LONG_EXTERNAL_POLICY)
OUTCOME_ENGINES=(OUTCOME_ENGINE,POLICY_OUTCOME_ENGINE,*EXTERNAL_LONG_ENGINES)
CONTINUATION_ENGINES=(CONTINUATION_ENGINE,*POLICY_ENGINES,OUTCOME_ENGINE,*EXTERNAL_LONG_ENGINES)
CONTROL_REASONS=frozenset({'MANUAL','PERMISSION_EXPIRED','SESSION_CHANGED','MARKET_UNAVAILABLE','DATA_INVALID',
    'RPC_UNAVAILABLE','QUOTE_UNAVAILABLE','OUTCOME_UNKNOWN','RECEIPT_INVALID','STATE_CHANGED','INTERNAL_ERROR',
    'RUN_PAUSED','THREAD_START_FAILED','SHUTDOWN'})


def _schema(db):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<64:
        raise DexExecutorError('Actualiza la bóveda para el diario causal del bot DEX')


def _cursor_json(cursor:DexBotCursor)->dict:
    return {**asdict(cursor),'last_checked_at':cursor.last_checked_at.isoformat()}


def _state_json(db,snapshot:ExecutorState)->dict:
    return {'anchor':_anchor(db,snapshot),'balance_usdt':str(snapshot.balance_usdt),'balance_wbnb':str(snapshot.balance_wbnb),
        'operator_eoa':snapshot.current_operator_code_empty}


def _decision_state(value,grant,creation_nonce,at):
    if set(value)!={'anchor','balance_usdt','balance_wbnb','operator_eoa'} or value['operator_eoa'] is not True:raise ValueError('state')
    counters=_validate_anchor(value['anchor'],creation_nonce,at)
    if str(counters['policy_id'])!=grant['policy_id']:raise ValueError('policy changed')
    return DexDecisionState(**{key:counters[key] for key in ('owned_wbnb','cost_usdt','buy_spent','entries','realized_loss','acquired_wbnb','last_entry_at')},
        balance_usdt=_uint(value['balance_usdt'],'USDT',positive=False),balance_wbnb=_uint(value['balance_wbnb'],'WBNB',positive=False),
        current_operator_code_empty=True,policy_limits=PolicyLimits.parse(grant['limits']),
        block_at=_time(datetime.fromisoformat(value['anchor']['block_at'])),observed_at=_time(datetime.fromisoformat(value['anchor']['observed_at'])))


def _quote(value):
    if set(value)!={'bid','ask','event_at','observed_at','symbol','source'}:raise ValueError('quote')
    return DexReferenceQuote(Decimal(value['bid']),Decimal(value['ask']),_time(datetime.fromisoformat(value['event_at'])),
        _time(datetime.fromisoformat(value['observed_at'])),value['symbol'],value['source'])


def _settled(db,grant_id,*,pending_ok=False,run_id=None,include_resolutions=False,as_of=None,include_external=False):
    values=[]
    for item in db.scalars(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==grant_id)):
        row,terms,state=operator.intent_record(db,item.id)
        if row.status=='ABANDONED_UNSENT':continue
        if run_id is not None:
            if terms['action']=='STOP':continue
            decision=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.payload_digest==terms['decision_ref']))
            if decision is None:raise DexExecutorError('Hay una ejecución sin procedencia causal verificable')
            if decision.run_id!=run_id:continue
        if include_resolutions:
            from app.portfolio.dex_operator_outcomes import operator_outcome
            settled=operator_outcome(db,row.id,as_of=as_of,include_external=include_external).settled
        else:settled=row.status in {'FINALIZED','REVERTED'}
        if not settled:
            if pending_ok:continue
            raise DexExecutorError('Hay una ejecución del bot pendiente de resolver')
        values.append((int(terms['nonce']),row.id))
    return [identifier for _,identifier in sorted(values)]


def _position(db,row,terms,identifiers,at,*,resolved_calls=None,include_external=False):
    from app.portfolio.dex_operator_outcomes import operator_outcome,outcome_witnesses,fold_operator_position
    if not isinstance(identifiers,list) or len(set(identifiers))!=len(identifiers):raise ValueError('settlements')
    entry=terms['resume']['initial_cursor']['entry_candle_t'] if terms['schema'] in CONTINUATION_ENGINES else None
    actual=outcome_witnesses(db,identifiers,as_of=at,include_external=include_external)
    if actual and resolved_calls is None or resolved_calls is not None and resolved_calls!=actual:raise ValueError('versioned resolution witnesses required')
    executions=[]
    for identifier in identifiers:
        item,recipe,state=operator.intent_record(db,identifier)
        fact=operator_outcome(db,identifier,as_of=at,include_external=include_external)
        ref=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.payload_digest==recipe['decision_ref']))
        if (item.account_id!=row.account_id or item.grant_id!=row.grant_id or ref is None or ref.run_id!=row.id
                or not fact.settled or fact.accepted_at>at
                or recipe['action'] not in {'BUY','SELL'}):raise ValueError('settled origin')
        executions.append((fact,_decoded(ref.payload_json,ref.payload_digest)['plan']['candle_t']))
    projection=fold_operator_position(terms['baseline']['counters'],tuple(executions),entry_candle_t=entry)
    return dict(projection.counters),projection.entry_candle_t


def _strategy_plan(db,terms,*,snapshot,**arguments):
    if terms['schema'] not in POLICY_ENGINES:return plan_strategy(snapshot=snapshot,**arguments)
    from app.portfolio.dex_controls import control_record
    carried=int(control_record(db,terms['policy_rollover_id'])[1]['bot_rollover']['consumed']['last_entry_at'])
    actual= snapshot if type(snapshot) is DexDecisionState else DexDecisionState.from_executor(snapshot)
    effective=max(actual.last_entry_at,carried)
    plan=plan_strategy(snapshot=replace(actual,last_entry_at=effective),**arguments)
    return replace(plan,evidence={**plan.evidence,'policy_rollover_id':terms['policy_rollover_id'],
        'physical_last_entry_at':str(actual.last_entry_at),'effective_last_entry_at':str(effective)})


def _replay_event(db,row,terms,grant,review,bars,cursor,settled_before,event):
    expected={'schema','run_id','sequence_no','previous_digest','claim_ref','at','candle','quote','snapshot',
        'settled_ids','exit_cap_units','plan','execution','cursor'}
    if terms['schema'] in OUTCOME_ENGINES:expected=expected|{'resolved_calls'}
    if (set(event)!=expected or event['schema']!=terms['schema'] or event['run_id']!=row.id or event['claim_ref']!=terms['claim_ref']):
        raise ValueError('decision scope')
    at=_time(datetime.fromisoformat(event['at']))
    if not row.created_at<=at<datetime.fromisoformat(terms['expires_at']) or at<cursor.last_checked_at:raise ValueError('decision clock')
    step=INTERVALS[review['source']['timeframe']]
    if event['candle'] is not None:
        if event['candle']['t']!=bars[-1].t+step:raise ValueError('new candle')
        added,gaps=validate_candles([bar.json() for bar in bars[-2:]]+[event['candle']],review['source']['timeframe'],now=int(at.timestamp()))
        if gaps:raise ValueError('gap')
        if event['candle']!=added[-1].json():raise ValueError('noncanonical candle')
        bars=[*bars,added[-1]]
    if not set(settled_before)<=set(event['settled_ids']):raise ValueError('lost settlement')
    position,entry=_position(db,row,terms,event['settled_ids'],at,resolved_calls=event['resolved_calls'] if terms['schema'] in OUTCOME_ENGINES else None,
        include_external=terms['schema']in EXTERNAL_LONG_ENGINES)
    observed=event['snapshot'];state=_decision_state(observed,grant,terms['creation_nonce'],at)
    if position!={key:int(value) for key,value in observed['anchor']['counters'].items()}:raise ValueError('unowned position/counters')
    cursor=replace(cursor,entry_candle_t=entry)
    plan=_strategy_plan(db,terms,definition=review['definition'],timeframe=review['source']['timeframe'],candles=bars,cursor=cursor,
        snapshot=state,quote=_quote(event['quote']),entry_units=int(review['amounts']['entry_units']),
        exit_cap_units=_uint(event['exit_cap_units'],'Cuota de salida',maximum=2**128-1,positive=False),now=at)
    if event['plan']!=plan.json():raise ValueError('decision replay')
    recipe=event['execution']
    if plan.action in {'BUY','SELL'}:
        if not isinstance(recipe,dict):raise ValueError('missing executable recipe')
        operator._validate_recipe(recipe,grant,at)
        expected_keys={'action','wallet_digest','owner_digest','creation_nonce','policy_id','contract_nonce','nonce','gas','gas_price',
            'max_gas_wei','operator_balance','anchor','trade'}
        if (set(recipe)!=expected_keys or recipe['action']!=plan.action or recipe['trade']['amount']!=str(plan.amount_units)
                or recipe['anchor']!=observed['anchor'] or recipe['creation_nonce']!=terms['creation_nonce']):raise ValueError('executable amount/scope')
    elif recipe is not None:raise ValueError('unexpected execution')
    next_cursor=DexBotCursor(bars[-1].t,at,entry,plan.trailing_high,plan.closing_reason)
    if event['cursor']!=_cursor_json(next_cursor):raise ValueError('cursor replay')
    return bars,next_cursor,event['settled_ids']


def run_record(db,identifier,*,verify_full=False):
    """Verify old formats unchanged and continuation ancestors iteratively."""
    from app.portfolio.dex_verified_read import read_record
    _schema(db);row=db.get(RealDexBotRun,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Sesión de bot DEX no encontrada')
    terms=_decoded(row.payload_json,row.payload_digest)
    if terms.get('schema') in DCA_ENGINES:
        from app.portfolio.dex_dca_journal import run_record as dca_record
        return dca_record(db,identifier,verify_full=verify_full)
    def verify(current):
        from app.portfolio.dex_bot_continuity import ordered_predecessors
        full=verify_full or current['force_full']
        if terms.get('schema') not in CONTINUATION_ENGINES:return _run_record(db,identifier,verify_full=full)
        verified={}
        for previous in ordered_predecessors(db,identifier):
            key=('run',previous)
            result=current['values'].get(key)
            if result is None:
                result=_run_record(db,previous,verify_full=full,parents=verified)
                current['values'][key]=result
            verified[previous]=result
        return verified[row.id]
    return read_record(db,'run',row.id,verify,warm=row.binding_id if terms.get('schema') in POLICY_ENGINES else None,
        force_full=verify_full)


def _run_record(db,identifier,*,verify_full=False,parents=None):
    _schema(db);row=db.get(RealDexBotRun,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Sesión de bot DEX no encontrada')
    terms,state=_decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
    try:
        keys={'schema','id','account_id','binding_id','grant_id','wallet_digest','claim_ref','connection_request_id',
                'expires_at','seed','baseline','creation_nonce','source_hash'}
        if terms['schema'] not in (ENGINE,EXTENDED_ENGINE,*CONTINUATION_ENGINES):raise ValueError('engine')
        if terms['schema']in EXTERNAL_LONG_ENGINES and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<78:
            raise ValueError('external outcome schema')
        permission=None
        if terms['schema'] in (EXTENDED_ENGINE,*CONTINUATION_ENGINES):
            keys=keys|{'permission'};permission=DexPermission.parse(terms['permission'])
        if terms['schema'] in CONTINUATION_ENGINES:keys=keys|{'resume'}
        if terms['schema'] in POLICY_ENGINES:keys=keys|{'policy_rollover_id'}
        if (set(terms) not in (keys,keys|{'request_digest'}) or 'request_digest' in terms and not _hex_ref(terms['request_digest'])
                or terms['id']!=row.id
                or terms['account_id']!=row.account_id or terms['binding_id']!=row.binding_id or terms['grant_id']!=row.grant_id
                or not _hex_ref(terms['wallet_digest']) or not _hex_ref(terms['claim_ref'])
                or str(UUID(terms['connection_request_id']))!=terms['connection_request_id']):raise ValueError('run scope')
        parent,binding,_=bindings.binding_record(db,row.binding_id);_,grant,_=operator.grant_record(db,row.grant_id)
        if ((binding['schema'] in SUCCESSOR_BINDINGS)!=(terms['schema'] in POLICY_ENGINES)
                or binding['schema']==RECOVERED_BINDING and terms['schema']not in(POLICY_OUTCOME_ENGINE,LONG_EXTERNAL_POLICY)
                or binding['schema']==EXTERNAL_BINDING and terms['schema']!=LONG_EXTERNAL_POLICY
                or terms['schema'] in POLICY_ENGINES and terms['policy_rollover_id']!=binding['rollover_id']):raise ValueError('policy engine scope')
        review=binding['origin']['review'];seed=binding['origin']['seed']
        if (parent.account_id!=row.account_id or parent.grant_id!=row.grant_id or terms['wallet_digest']!=binding['wallet_digest']
                or parent.created_at>row.created_at or review['source_kind']!='STRATEGY' or terms['source_hash']!=content_hash(review['source'])
                or terms['seed'][:len(seed)]!=seed or len(terms['seed'])<len(seed)
                or terms['creation_nonce']!=deployment_record(db,parent.deployment_id)[1]['nonce']):raise ValueError('source')
        expires=_time(datetime.fromisoformat(terms['expires_at']))
        if (not row.created_at<expires<=datetime.fromtimestamp(grant['limits']['expires_at'],row.created_at.tzinfo)
                or (expires-row.created_at).total_seconds()>(permission.requested_seconds if permission else LEGACY_PERMISSION_SECONDS)):
            raise ValueError('permission expiry')
        _validate_anchor(terms['baseline'],terms['creation_nonce'],row.created_at)
        initial=None
        if terms['schema'] in CONTINUATION_ENGINES:
            from app.portfolio.dex_bot_continuity import resume_material
            previous=parents[terms['resume']['previous_run_id']]
            if previous[1]['schema']in EXTERNAL_LONG_ENGINES and terms['schema']not in EXTERNAL_LONG_ENGINES:
                raise ValueError('external outcome downgrade')
            if terms['schema'] in POLICY_ENGINES:
                from app.portfolio.dex_bot_rollover import resume_rollover_material
                expected,initial=resume_rollover_material(db,previous,binding=binding,grant=grant,anchor=terms['baseline'],
                    seed=terms['seed'],at=row.created_at,child_id=row.id,
                    **({'include_resolutions':True,'include_external':terms['schema']in EXTERNAL_LONG_ENGINES} if terms['schema']in(POLICY_OUTCOME_ENGINE,LONG_EXTERNAL_POLICY) else {}))
            else:
                if previous[0].binding_id!=row.binding_id or previous[0].grant_id!=row.grant_id:raise ValueError('predecessor scope')
                expected,initial=resume_material(db,previous,anchor=terms['baseline'],seed=terms['seed'],at=row.created_at,child_id=row.id,
                    include_resolutions=terms['schema'] in OUTCOME_ENGINES,include_external=terms['schema']in EXTERNAL_LONG_ENGINES)
            if terms['resume']!=expected:raise ValueError('continuation baseline/cursor')
        elif terms['baseline']['counters']!=grant['anchor']['counters']:raise ValueError('nonempty initial position')
        # This profile also binds a financial archive; replay it until all its
        # dependencies are covered by the persistent in-memory fingerprint.
        cache=None if verify_full or terms['schema']in EXTERNAL_LONG_ENGINES else db.info.get('orbis_dex_replay_cache')
        context=(row.payload_digest,row.created_at,_encoded(review)[1],_encoded(grant)[1],operator._identity(db,'replay-cache','v1'))
        items=list(db.scalars(select(RealDexBotDecision).where(RealDexBotDecision.run_id==row.id).order_by(RealDexBotDecision.sequence_no)))
        prefix=cache.get(row.id) if isinstance(cache,dict) else None
        if isinstance(prefix,VerifiedPrefix) and prefix.matches(db,context,items):
            bars=list(prefix.bars);cursor=prefix.cursor;previous=prefix.digest;settled=list(prefix.settled);sequence=prefix.sequence
            last_plan=None if prefix.last_action is None else {'action':prefix.last_action,'reason':prefix.last_reason}
            offset=len(prefix.decisions)
        else:
            bars,gaps=validate_candles(terms['seed'],review['source']['timeframe'],now=int(row.created_at.timestamp()))
            step=INTERVALS[review['source']['timeframe']]
            if (gaps or terms['seed']!=[bar.json() for bar in bars]
                    or bars[-1].t!=int(row.created_at.timestamp())//step*step-step):raise ValueError('initial coverage')
            cursor=initial or DexBotCursor(bars[-1].t,row.created_at,None,None,None)
            previous=row.payload_digest;settled=[];sequence=-1;last_plan=None;offset=0
        for item in items[offset:]:
            event=_decoded(item.payload_json,item.payload_digest)
            if (str(UUID(item.id))!=item.id or type(event['sequence_no']) is not int
                    or item.sequence_no!=sequence+1 or item.previous_digest!=previous or event['sequence_no']!=item.sequence_no
                    or event['previous_digest']!=previous or event['at']!=item.created_at.isoformat()):raise ValueError('decision chain')
            bars,cursor,settled=_replay_event(db,row,terms,grant,review,bars,cursor,settled,event)
            last_plan=event['plan']
            previous=item.payload_digest;sequence=item.sequence_no
        if (set(state)!={'status','history','head','control_reason'} or state['status']!=row.status or row.status not in {'ACTIVE','PAUSED','STOPPED'}
                or type(state['head']['sequence_no']) is not int
                or state['head']!={'sequence_no':sequence,'digest':previous,'cursor':_cursor_json(cursor)}):raise ValueError('head')
        history=state['history'];statuses=[item['status'] for item in history]
        stamps=[_time(datetime.fromisoformat(item['at'])) for item in history]
        if (not 1<=len(history)<=3 or any(set(item)!={'status','at'} for item in history) or statuses[0]!='ACTIVE'
                or statuses[-1]!=row.status or len(set(statuses))!=len(statuses) or stamps!=sorted(stamps)
                or stamps[0]!=row.created_at or any(stamp.isoformat()!=event['at'] for stamp,event in zip(stamps,history))
                or row.updated_at!=max(cursor.last_checked_at,stamps[-1])
                or any(b not in {'ACTIVE':{'PAUSED','STOPPED'},'PAUSED':{'STOPPED'}}.get(a,set()) for a,b in zip(statuses,statuses[1:]))):
            raise ValueError('run history')
        if row.status!='ACTIVE' and cursor.last_checked_at>stamps[-1]:raise ValueError('decisions after pause')
        if ((last_plan is not None and last_plan['action']=='PAUSE')!=('PAUSED' in statuses)
                or row.status=='ACTIVE' and state['control_reason'] is not None
                or row.status=='PAUSED' and state['control_reason']!=last_plan['reason']
                or row.status=='STOPPED' and state['control_reason'] not in CONTROL_REASONS):raise ValueError('control reason')
        if isinstance(cache,dict):
            remember(cache,row.id,VerifiedPrefix(context,tuple(witness(item) for item in items),receipt_witnesses(db,tuple(settled)),
                tuple(bars),cursor,tuple(settled),previous,sequence,last_plan['action'] if last_plan else None,last_plan['reason'] if last_plan else None))
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,OverflowError,InvalidOperation):
        raise DexExecutorError('El archivo causal del bot DEX no es coherente') from None
    return row,terms,state,{'bars':bars,'cursor':cursor,'settled':settled,'review':review,'grant':grant}


def _public(row,terms,state):
    if terms['schema'] in DCA_ENGINES:
        from app.portfolio.dex_dca_journal import _public as dca_public
        return dca_public(row,terms,state)
    permission=DexPermission.parse(terms['permission']) if terms['schema'] in (EXTENDED_ENGINE,*CONTINUATION_ENGINES) else None
    return {'id':row.id,'account_id':row.account_id,'binding_id':row.binding_id,'grant_id':row.grant_id,'stored_status':row.status,
        'expires_at':terms['expires_at'],'head':state['head'],'reason':state['control_reason'],
        'engine':terms['schema'],'permission_mode':permission.mode if permission else 'SESSION',
        'maximum_permission_seconds':permission.requested_seconds if permission else LEGACY_PERMISSION_SECONDS,
        **({'previous_run_id':terms['resume']['previous_run_id'],'observation_gap':terms['resume']['observation_gap']}
           if terms['schema'] in CONTINUATION_ENGINES else {}),
        **({'policy_rollover_id':terms['policy_rollover_id']} if terms['schema'] in POLICY_ENGINES else {}),
        'execution_enabled':False,'worker_required':True,'automatic_execution':False}


def start_run(db,*,identifier,account_id,binding_id,wallet_address,connection,snapshot,seed,now,request_digest=None,
              permission_mode=None,requested_seconds=None,previous_run_id=None,activation_window_seconds=900,research_check=None):
    candidate=db.get(RealDexBotBinding,str(UUID(binding_id)))
    if candidate is not None:
        payload=_decoded(candidate.payload_json,candidate.payload_digest);origin=payload.get('origin')
        review=origin.get('review') if isinstance(origin,dict) else None
        if isinstance(review,dict) and review.get('source_kind')=='DCA':
            from app.portfolio.dex_dca_journal import start_run as start_dca
            return start_dca(db,identifier=identifier,account_id=account_id,binding_id=binding_id,wallet_address=wallet_address,
                connection=connection,snapshot=snapshot,seed=seed,now=now,request_digest=request_digest,
                permission_mode=permission_mode,requested_seconds=requested_seconds,previous_run_id=previous_run_id,
                activation_window_seconds=activation_window_seconds,research_check=research_check)
    if activation_window_seconds!=900:raise DexExecutorError('La ventana de activación corresponde sólo a DCA')
    now=_time(now);identifier,account_id,binding_id=[str(UUID(v)) for v in (identifier,account_id,binding_id)]
    wallet=canonical_wallet_address(wallet_address)
    if request_digest is not None and not _hex_ref(request_digest):raise DexExecutorError('Huella de activación no válida')
    permission=None
    if permission_mode is not None:
        permission=DexPermission(permission_mode,requested_seconds);permission.validate()
    elif requested_seconds is not None:raise DexExecutorError('Elige el modo del permiso antes de ampliar su vigencia')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');_schema(db)
    prior=db.get(RealDexBotRun,identifier)
    if prior is not None:
        row,terms,state,_=run_record(db,identifier)
        if ((row.account_id,row.binding_id,terms['wallet_digest'])!=(account_id,binding_id,operator._identity(db,'wallet',wallet))
                or terms.get('request_digest')!=request_digest or terms.get('permission')!=(permission.json() if permission else None)
                or terms.get('resume',{}).get('previous_run_id')!=previous_run_id):
            raise DexExecutorError('ID de sesión ya utilizado')
        # Read-only recovery never returns the lost RAM claim or starts again.
        return _public(row,terms,state),None
    if type(connection) is not DexOperatorConnection:raise DexExecutorError('Conecta la operadora verificada antes de activar')
    live=connection.status(account_id=account_id,wallet_address=wallet,now=now)
    if not live['connected'] or live['binding_id']!=binding_id:raise DexExecutorError('La firma no está vinculada a este bot')
    parent,binding,_=bindings.binding_record(db,binding_id);grant_row,grant,_=operator.grant_record(db,parent.grant_id)
    require_settled_policy_controls(db,parent.deployment_id)
    renewed_policy=binding['schema'] in SUCCESSOR_BINDINGS
    if renewed_policy and (previous_run_id is None or permission is None):
        raise DexExecutorError('La política renovada requiere una continuidad y un permiso explícitos')
    if (parent.account_id!=account_id or live['grant_id']!=parent.grant_id or binding['wallet_digest']!=operator._identity(db,'wallet',wallet)
            or parent.status!='READY' or grant_row.status!='ACTIVE' or now<parent.created_at
            or binding['origin']['review']['source_kind']!='STRATEGY'):
        raise DexExecutorError('El vínculo o el tipo de estrategia no está preparado para este motor')
    _context(db,parent.deployment_id,account_id,wallet,snapshot,now)
    if (snapshot.finalized is not True or snapshot.policy_limits is None or snapshot.policy_limits.as_dict()!=grant['limits']
            or operator._identity(db,'wallet',snapshot.operator)!=grant['operator_digest']
            or previous_run_id is None and _anchor(db,snapshot)['counters']!=grant['anchor']['counters']):
        raise DexExecutorError('La política o posición inicial cambió')
    try:
        review=binding['origin']['review'];origin_seed=binding['origin']['seed'];step=INTERVALS[review['source']['timeframe']]
        bars,gaps=validate_candles(seed,review['source']['timeframe'],now=int(now.timestamp()))
        if (gaps or len(seed)<len(origin_seed) or seed[:len(origin_seed)]!=origin_seed
                or seed!=[bar.json() for bar in bars] or bars[-1].t!=int(now.timestamp())//step*step-step):raise ValueError('seed')
        expiry=_time(datetime.fromisoformat(live['expires_at']))
        if not now<expiry or (expiry-now).total_seconds()>(permission.requested_seconds if permission else LEGACY_PERMISSION_SECONDS):raise ValueError('permission')
    except (ValueError,KeyError,TypeError,IndexError,AttributeError):
        raise DexExecutorError('El histórico inicial o la vigencia de activación no es válido') from None
    if db.scalar(select(RealDexBotRun.id).where(RealDexBotRun.binding_id==binding_id,RealDexBotRun.status=='ACTIVE')):
        raise DexExecutorError('Este vínculo ya tiene una sesión; revisa su estado antes de rearmar')
    resume=None;cursor=None
    if previous_run_id is not None:
        from app.portfolio.dex_bot_continuity import resume_material
        if permission is None:raise DexExecutorError('El rearme requiere un permiso nuevo explícito')
        previous_run_id=str(UUID(previous_run_id));previous=run_record(db,previous_run_id)
        if previous[0].account_id!=account_id or not renewed_policy and (previous[0].binding_id!=binding_id or previous[0].grant_id!=parent.grant_id):
            raise DexExecutorError('El permiso anterior pertenece a otro bot')
        # Any unresolved intent, even from another old epoch, blocks signing.
        external_support=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78
        _settled(db,parent.grant_id,include_resolutions=True,as_of=now,include_external=external_support)
        if renewed_policy:
            from app.portfolio.dex_bot_rollover import resume_rollover_material
            from app.portfolio.dex_operator_outcomes import outcome_witnesses
            prior_ids=_settled(db,previous[0].grant_id,include_resolutions=True,as_of=now,include_external=external_support)
            references=outcome_witnesses(db,prior_ids,as_of=now,include_external=external_support)
            use_external=(binding['schema']==EXTERNAL_BINDING or previous[1]['schema']in EXTERNAL_LONG_ENGINES
                or any('external_review'in item for item in references))
            use_outcomes=(binding['schema']in(RECOVERED_BINDING,EXTERNAL_BINDING) or previous[1]['schema'] in OUTCOME_ENGINES
                or bool(references))
            resume,cursor=resume_rollover_material(db,previous,binding=binding,grant=grant,anchor=_anchor(db,snapshot),seed=seed,at=now,child_id=identifier,
                **({'include_resolutions':True,'include_external':use_external} if use_outcomes else {}))
        else:
            from app.portfolio.dex_operator_outcomes import outcome_witnesses
            settled=_settled(db,parent.grant_id,run_id=previous[0].id,include_resolutions=True,as_of=now,include_external=external_support)
            references=outcome_witnesses(db,settled,as_of=now,include_external=external_support)
            use_external=previous[1]['schema']in EXTERNAL_LONG_ENGINES or any('external_review'in item for item in references)
            use_outcomes=bool(references) or previous[1]['schema'] in OUTCOME_ENGINES
            resume,cursor=resume_material(db,previous,anchor=_anchor(db,snapshot),seed=seed,at=now,child_id=identifier,
                include_resolutions=use_outcomes,include_external=use_external)
    elif _settled(db,parent.grant_id):raise DexExecutorError('La primera activación no adopta ejecuciones anteriores')
    claim=str(uuid4());terms={'schema':LONG_EXTERNAL_POLICY if renewed_policy and resume and use_external else LONG_EXTERNAL_OUTCOME if resume and not renewed_policy and use_external else POLICY_OUTCOME_ENGINE if renewed_policy and resume and 'resolved_calls' in resume else POLICY_ENGINE if renewed_policy else OUTCOME_ENGINE if resume and 'resolved_calls' in resume else CONTINUATION_ENGINE if resume else EXTENDED_ENGINE if permission else ENGINE,'id':identifier,'account_id':account_id,'binding_id':binding_id,'grant_id':parent.grant_id,
        'wallet_digest':binding['wallet_digest'],'claim_ref':operator._identity(db,'bot-claim',claim),'connection_request_id':live['request_id'],
        'expires_at':live['expires_at'],'seed':seed,'baseline':_anchor(db,snapshot),
        'creation_nonce':deployment_record(db,parent.deployment_id)[1]['nonce'],'source_hash':content_hash(binding['origin']['review']['source'])}
    if request_digest is not None:terms['request_digest']=request_digest
    if permission is not None:terms['permission']=permission.json()
    if resume is not None:terms['resume']=resume
    if renewed_policy:terms['policy_rollover_id']=binding['rollover_id']
    raw,digest=_encoded(terms)
    cursor=cursor or DexBotCursor(seed[-1]['t'],now,None,None,None)
    state={'status':'ACTIVE','control_reason':None,'history':[{'status':'ACTIVE','at':now.isoformat()}],
        'head':{'sequence_no':-1,'digest':digest,'cursor':_cursor_json(cursor)}}
    state_raw,state_digest=_encoded(state)
    row=RealDexBotRun(id=identifier,account_id=account_id,binding_id=binding_id,grant_id=parent.grant_id,payload_json=raw,payload_digest=digest,
        state_json=state_raw,state_digest=state_digest,status='ACTIVE',created_at=now,updated_at=now)
    db.add(row);db.flush();run_record(db,identifier)
    if research_check is not None:
        from app.portfolio.dex_research import save_authorization_qualification
        save_authorization_qualification(db,identifier,research_check)
    db.commit();return _public(row,terms,state),claim


def _decision_context(db,identifier,*,account_id,claim,candles,quote,snapshot,exit_cap_units,now,expected_head=None):
    if type(snapshot) is not ExecutorState:raise DexExecutorError('Revisa el estado actual del ejecutor')
    now=_time(now);row,terms,state,archive=run_record(db,identifier)
    if terms['schema'] in DCA_ENGINES:
        from app.portfolio.dex_dca_journal import _decision_context as dca_context
        return dca_context(db,identifier,account_id=account_id,claim=claim,candles=candles,quote=quote,
            snapshot=snapshot,exit_cap_units=exit_cap_units,now=now,expected_head=expected_head)
    if (row.account_id!=account_id or row.status!='ACTIVE' or operator._identity(db,'bot-claim',str(UUID(claim)))!=terms['claim_ref']
            or now>=datetime.fromisoformat(terms['expires_at'])):raise DexExecutorError('El permiso del bot cambió, terminó o caducó')
    if expected_head is not None and state['head']['digest']!=expected_head:raise DexExecutorError('La revisión del bot cambió durante la consulta')
    parent,binding,_=bindings.binding_record(db,row.binding_id);grant_row,grant,_=operator.grant_record(db,row.grant_id)
    require_settled_policy_controls(db,parent.deployment_id)
    _context(db,parent.deployment_id,account_id,snapshot.owner,snapshot,now)
    if (parent.status!='READY' or grant_row.status!='ACTIVE' or snapshot.policy_limits is None or snapshot.policy_limits.as_dict()!=grant['limits']
            or operator._identity(db,'wallet',snapshot.owner)!=terms['wallet_digest']
            or operator._identity(db,'wallet',snapshot.operator)!=grant['operator_digest']):raise DexExecutorError('La autoridad del ejecutor cambió')
    prior=[bar.json() for bar in archive['bars']]
    if (not isinstance(candles,list) or len(candles) not in (len(prior),len(prior)+1) or candles[:len(prior)]!=prior):
        raise DexExecutorError('Las velas cambiaron o falta continuidad; no se operará retrospectivamente')
    resolved=terms['schema'] in OUTCOME_ENGINES
    external=terms['schema']in EXTERNAL_LONG_ENGINES
    settled=_settled(db,row.grant_id,run_id=row.id,include_resolutions=resolved,as_of=now,include_external=external)
    from app.portfolio.dex_operator_outcomes import outcome_witnesses
    position,entry=_position(db,row,terms,settled,now,resolved_calls=outcome_witnesses(db,settled,as_of=now,include_external=external) if resolved else None,
        include_external=external)
    if position!={key:int(value) for key,value in _anchor(db,snapshot)['counters'].items()}:
        raise DexExecutorError('La posición del ejecutor no coincide con los recibos del bot')
    bars,gaps=validate_candles(candles,archive['review']['source']['timeframe'],now=int(now.timestamp()))
    if gaps or candles!=[bar.json() for bar in bars]:raise DexExecutorError('Faltan velas de mercado o contienen columnas no admitidas')
    cursor=replace(archive['cursor'],entry_candle_t=entry)
    plan=_strategy_plan(db,terms,definition=archive['review']['definition'],timeframe=archive['review']['source']['timeframe'],candles=bars,
        cursor=cursor,snapshot=snapshot,quote=quote,entry_units=int(archive['review']['amounts']['entry_units']),exit_cap_units=exit_cap_units,now=now)
    return row,terms,state,archive,settled,bars,entry,plan


def preview_decision(db,identifier,*,account_id,claim,candles,quote,snapshot,exit_cap_units,now,expected_head=None):
    """Read-only preparation; the write and emission gates repeat these checks."""
    return _decision_context(db,identifier,account_id=account_id,claim=claim,candles=candles,quote=quote,snapshot=snapshot,
        exit_cap_units=exit_cap_units,now=now,expected_head=expected_head)[-1]


def record_decision(db,identifier,*,account_id,claim,candles,quote,snapshot,execution,exit_cap_units,now,expected_head=None):
    candidate=db.get(RealDexBotRun,str(UUID(identifier)))
    if candidate is not None and _decoded(candidate.payload_json,candidate.payload_digest).get('schema') in DCA_ENGINES:
        from app.portfolio.dex_dca_journal import record_decision as record_dca
        return record_dca(db,identifier,account_id=account_id,claim=claim,candles=candles,quote=quote,snapshot=snapshot,
            execution=execution,exit_cap_units=exit_cap_units,now=now,expected_head=expected_head)
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    row,terms,state,archive,settled,bars,entry,plan=_decision_context(db,identifier,account_id=account_id,claim=claim,
        candles=candles,quote=quote,snapshot=snapshot,exit_cap_units=exit_cap_units,now=now,expected_head=expected_head)
    grant=archive['grant'];prior=[bar.json() for bar in archive['bars']]
    recipe=None
    if plan.action in {'BUY','SELL'}:
        if type(execution) is not DexTradePlan or execution.snapshot!=snapshot:raise DexExecutorError('Simula el plan exacto antes de registrar la decisión')
        execution.validate(now);recipe=operator._recipe(db,execution)
    elif execution is not None:raise DexExecutorError('Esta decisión no autoriza una ejecución')
    sequence=state['head']['sequence_no']+1
    event={'schema':terms['schema'],'run_id':row.id,'sequence_no':sequence,'previous_digest':state['head']['digest'],'claim_ref':terms['claim_ref'],
        'at':now.isoformat(),'candle':candles[-1] if len(candles)>len(prior) else None,'quote':quote.json(),'snapshot':_state_json(db,snapshot),
        'settled_ids':settled,'exit_cap_units':str(exit_cap_units),'plan':plan.json(),'execution':recipe,
        'cursor':_cursor_json(DexBotCursor(bars[-1].t,now,entry,plan.trailing_high,plan.closing_reason))}
    if terms['schema'] in OUTCOME_ENGINES:
        from app.portfolio.dex_operator_outcomes import outcome_witnesses
        event['resolved_calls']=outcome_witnesses(db,settled,as_of=now,include_external=terms['schema']in EXTERNAL_LONG_ENGINES)
    _replay_event(db,row,terms,grant,archive['review'],archive['bars'],archive['cursor'],archive['settled'],event)
    raw,digest=_encoded(event);db.add(RealDexBotDecision(id=str(uuid4()),run_id=row.id,sequence_no=sequence,previous_digest=state['head']['digest'],
        payload_json=raw,payload_digest=digest,created_at=now))
    state['head']={'sequence_no':sequence,'digest':digest,'cursor':event['cursor']}
    if plan.action=='PAUSE':
        row.status=state['status']='PAUSED';state['control_reason']=plan.reason;state['history'].append({'status':'PAUSED','at':now.isoformat()})
    row.updated_at=now;row.state_json,row.state_digest=_encoded(state);db.flush();run_record(db,row.id);db.commit()
    return {'run_id':row.id,'decision_ref':digest,'plan':plan.json(),'sequence_no':sequence}


def stop_run(db,identifier,*,account_id,now,reason='MANUAL'):
    if reason not in CONTROL_REASONS:raise DexExecutorError('Motivo de parada no admitido')
    now=_time(now);db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state,_=run_record(db,identifier)
    if row.account_id!=account_id or now<row.updated_at:raise DexExecutorError('Cuenta o reloj del bot distinto')
    if row.status!='STOPPED':
        row.status=state['status']='STOPPED';row.updated_at=now;state['control_reason']=reason
        state['history'].append({'status':'STOPPED','at':now.isoformat()})
        row.state_json,row.state_digest=_encoded(state);db.flush();run_record(db,row.id);db.commit()
    return _public(row,terms,state)


def require_decision(db,grant_id,decision_ref,recipe,now):
    item=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.payload_digest==decision_ref))
    if item is None:raise DexExecutorError('El vínculo requiere activación del bot y una decisión causal antes de operar')
    row,terms,state,archive=run_record(db,item.run_id);event=_decoded(item.payload_json,item.payload_digest)
    require_settled_policy_controls(db,archive['grant']['deployment_id'])
    if (row.grant_id!=grant_id or row.status!='ACTIVE' or now is None or recipe is None or event['execution'] is None
            or state['head']['digest']!=decision_ref or not 0<=(_time(now)-item.created_at).total_seconds()<=15
            or _time(now)>=datetime.fromisoformat(terms['expires_at'])
            or any(recipe.get(key)!=value for key,value in event['execution'].items())):
        raise DexExecutorError('La decisión causal cambió, caducó o no coincide con esta ejecución')


def verify_all_runs(db,*,version):
    names=('real_dex_bot_runs','real_dex_bot_decisions')
    if version<64:
        tables={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(name in tables and db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one() for name in names):
            raise DexExecutorError('Una bóveda anterior no admite sesiones causales DEX')
        return
    active=set();archives={}
    for row in db.scalars(select(RealDexBotRun)):
        archives[row.id]=run_record(db,row.id,verify_full=True)
        if row.status=='ACTIVE':
            if row.binding_id in active:raise DexExecutorError('Dos sesiones activas para el mismo vínculo')
            active.add(row.binding_id)
    for row in db.scalars(select(RealDexBotDecision)):
        if db.get(RealDexBotRun,row.run_id) is None:raise DexExecutorError('Decisión DEX sin sesión')
    bound_grants=set(db.scalars(select(RealDexBotBinding.grant_id)))
    for item in db.scalars(select(RealDexOperatorIntent)):
        intent,recipe,intent_state=operator.intent_record(db,item.id)
        if intent.grant_id not in bound_grants or recipe['action']=='STOP':continue
        decision=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.payload_digest==recipe['decision_ref']))
        if decision is None or decision.run_id not in archives:raise DexExecutorError('Ejecución DEX sin decisión causal')
        run,terms,state,_=archives[decision.run_id];event=_decoded(decision.payload_json,decision.payload_digest)
        if (run.grant_id!=intent.grant_id or event['execution'] is None
                or not 0<=(intent.created_at-decision.created_at).total_seconds()<=15
                or any(recipe.get(key)!=value for key,value in event['execution'].items())):
            raise DexExecutorError('La ejecución DEX no reproduce su decisión causal')
        stopped=datetime.fromisoformat(state['history'][1]['at']) if len(state['history'])>1 else None
        for transition in intent_state['history']:
            if transition['status']=='BROADCAST_ATTEMPTED':
                stamp=datetime.fromisoformat(transition['at'])
                if (stopped is not None and stamp>stopped or stamp>=datetime.fromisoformat(terms['expires_at'])
                        or not 0<=(stamp-decision.created_at).total_seconds()<=15):
                    raise DexExecutorError('La emisión DEX no tenía un permiso causal vigente')
