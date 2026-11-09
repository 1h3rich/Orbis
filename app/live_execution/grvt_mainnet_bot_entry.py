"""Concesión y entrada causal MAINNET. No arranca trabajadores ni restaura autoridad.

El coordinador debe aportar un permiso RAM revocable en cada llamada. La
selección queda quemada antes del claim/POST, incluso si se pierde la respuesta.
"""
from dataclasses import asdict,dataclass
from decimal import Decimal,localcontext,ROUND_CEILING,ROUND_FLOOR
import hashlib,json,re,time
from uuid import UUID,uuid4
from sqlalchemy import select

from app.live_execution.grvt_mainnet_bracket import (GrvtMainnetError,MainnetBracketDraft,
    decimal_amount,unsigned_mainnet_bracket,MAX_ENTRY_CAP,MAX_EXIT_CAP)
from app.live_execution.grvt_mainnet_journal import encode,_draft,_instrument,_control
from app.live_execution.grvt_mainnet_transport import fetch_mainnet_instrument,TICKER_URL,_result
from app.live_execution.grvt_mainnet_preflight import number
from app.live_execution.grvt_bot_source import bot_source,next_bot_open,verify_bot_source
from app.live_execution.grvt_paper_binding import reviewed_paper_signal,SIGNAL_MAX_AGE_NS
from app.portfolio.models import (GrvtMainnetBotRun,GrvtMainnetBotEvent,GrvtMainnetBotSelection,GrvtMainnetBracket)

_TABLES={'grvt_mainnet_bot_runs','grvt_mainnet_bot_events','grvt_mainnet_bot_selections'}

@dataclass(frozen=True,slots=True)
class MainnetBotPolicy:
    sub_account_id:str
    entry_cap:Decimal
    exit_cap:Decimal
    total_entry_cap:Decimal
    loss_cap:Decimal
    max_entries:int=1
    wait_seconds:int=120
    exit_seconds:int=120
    recovery_seconds:int=180
    max_attempts:int=3
    max_spread_bps:int=25
    max_slippage_bps:int=25

    def __post_init__(self):
        if (not isinstance(self.sub_account_id,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',self.sub_account_id)
                or int(self.sub_account_id)>=2**64):raise GrvtMainnetError('Subcuenta del bot inválida')
        for key in ('entry_cap','exit_cap','total_entry_cap','loss_cap'):decimal_amount(getattr(self,key))
        if (self.entry_cap>MAX_ENTRY_CAP or self.exit_cap>MAX_EXIT_CAP or self.total_entry_cap>MAX_EXIT_CAP
                or self.entry_cap>self.total_entry_cap or self.loss_cap>self.total_entry_cap):raise GrvtMainnetError('Topes del bot inválidos')
        for key,lo,hi in (('max_entries',1,20),('wait_seconds',30,86400),('exit_seconds',120,86400),
                ('recovery_seconds',30,300),('max_attempts',1,3),('max_spread_bps',0,100),('max_slippage_bps',0,100)):
            if type(getattr(self,key)) is not int or not lo<=getattr(self,key)<=hi:raise GrvtMainnetError('Límite temporal o de ejecución del bot inválido')
        if self.wait_seconds+self.exit_seconds+self.recovery_seconds>86400:raise GrvtMainnetError('La concesión supera veinticuatro horas')


def _policy(raw):
    if set(raw)!=set(MainnetBotPolicy.__dataclass_fields__):raise ValueError('bot policy fields')
    return MainnetBotPolicy(**{key:number(value) if key.endswith('_cap') else value for key,value in raw.items()})


def _stamp(at):
    if type(at) is not int or not 0<at<2**63:raise ValueError('bot timestamp')
    return at


def bot_schema(db,*,required=False):
    tables={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    version=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    if _TABLES<=tables:return True
    if required or version>=53 or _TABLES&tables:raise GrvtMainnetError('Diario del bot MAINNET truncado')
    return False


def signal_parent(source,action):
    material=('orbis-grvt-mainnet-bot-signal-v1:'+source['session_id']+':'+action).encode('ascii')
    return str(2**63 | int.from_bytes(hashlib.sha256(material).digest()[:8],'big')),hashlib.sha256(material).hexdigest()


def _read(db,row,*,groups=True):
    if row is None:raise GrvtMainnetError('Concesión MAINNET no encontrada')
    from app.live_execution.grvt_perp_bot_grant import is_perp_grant,read_perp_bot_grant
    if is_perp_grant(row.payload_json):
        return read_perp_bot_grant(db,row,groups=groups)
    value=json.loads(row.payload_json)
    researched=value.get('schema')=='orbis.grvt.mainnet.bot.grant.v3'
    operational=value.get('schema') in ('orbis.grvt.mainnet.bot.grant.v2','orbis.grvt.mainnet.bot.grant.v3')
    if (set(value)!={'schema','id','network','source','policy','started_at_ns','entry_deadline_ns','deadline_ns'}|({'keep_operating'} if operational else set())|({'research_binding'} if researched else set())
            or value['schema'] not in ('orbis.grvt.mainnet.bot.grant.v1','orbis.grvt.mainnet.bot.grant.v2','orbis.grvt.mainnet.bot.grant.v3')
            or operational and type(value['keep_operating']) is not bool or value['network']!='MAINNET'
            or str(UUID(value['id']))!=row.id or encode(value)!=(row.payload_json,row.payload_digest)):
        raise ValueError('bot grant integrity')
    policy=_policy(value['policy']);verify_bot_source(value['source']);start=_stamp(value['started_at_ns'])
    if researched:
        from app.research.grvt_binding import read_grvt_research_binding
        research=read_grvt_research_binding(value['research_binding'],value['source'])
        if research.paper.last_observed_at_ns>start:raise ValueError('PAPER evidence after authorization')
    if (row.sub_account_id!=policy.sub_account_id or start<value['source']['session_started_at_ns']
            or _stamp(value['entry_deadline_ns'])!=start+policy.wait_seconds*1_000_000_000
            or _stamp(value['deadline_ns'])!=value['entry_deadline_ns']+(policy.exit_seconds+policy.recovery_seconds)*1_000_000_000):
        raise ValueError('bot grant scope')
    selections=[];previous=None;last=start;terminal=None;seen=set();financial_outcomes=[]
    for record in db.scalars(select(GrvtMainnetBotEvent).where(GrvtMainnetBotEvent.run_id==row.id).order_by(GrvtMainnetBotEvent.sequence)):
        event=json.loads(record.payload_json)
        if (set(event)!={'run_id','header_digest','sequence','previous_digest','at_ns','type','details'}
                or event['run_id']!=row.id or event['header_digest']!=row.payload_digest
                or type(event['sequence']) is not int or event['sequence']!=len(seen)+1 or record.sequence!=event['sequence']
                or event['previous_digest']!=previous or encode(event)!=(record.payload_json,record.payload_digest)
                or _stamp(event['at_ns'])<last or terminal is not None):raise ValueError('bot event chain')
        detail=event['details'];kind=event['type']
        if kind=='SELECT':
            if (set(detail)!={'action_request_id','recorded_at_ns','parent_client_order_id','signal_ref','draft','instrument','quote'}
                    or str(UUID(detail['action_request_id']))!=detail['action_request_id']
                    or signal_parent(value['source'],detail['action_request_id'])!=(detail['parent_client_order_id'],detail['signal_ref'])
                    or not start<_stamp(detail['recorded_at_ns'])<=event['at_ns']<=value['entry_deadline_ns']
                    or event['at_ns']-detail['recorded_at_ns']>SIGNAL_MAX_AGE_NS
                    or selections and (selections[-1]['settlement'] is None
                        or operational and detail['recorded_at_ns']<=selections[-1]['settled_at_ns']) or len(selections)>=policy.max_entries):
                raise ValueError('bot signal selection')
            draft=_draft(detail['draft']);instrument=_instrument(detail['instrument'])
            if draft!=_entry_draft(policy,value['source'],instrument,detail['quote'],event['at_ns']):raise ValueError('bot strategy percentages changed')
            if (draft.sub_account_id!=policy.sub_account_id or draft.instrument!=value['source']['instrument']
                    or draft.direction!=value['source']['definition']['direction']
                    or any(getattr(draft,key)!=getattr(policy,key) for key in ('entry_cap','exit_cap','exit_seconds','max_spread_bps','max_slippage_bps'))):
                raise ValueError('bot entry terms changed')
            unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=detail['parent_client_order_id'],nonces=(1,2,3),issued_at_ns=event['at_ns'])
            with localcontext() as context:
                context.prec=100
                if policy.entry_cap*(len(selections)+1)>policy.total_entry_cap:raise ValueError('bot budget exceeded')
                if -sum((number(item['settlement']['net_realized_pnl_usdt']) for item in selections),Decimal(0))>=policy.loss_cap:
                    raise ValueError('bot loss limit exceeded')
            reservation=db.get(GrvtMainnetBotSelection,detail['parent_client_order_id'])
            if (reservation is None or reservation.run_id!=row.id or reservation.sequence!=record.sequence
                    or reservation.signal_ref!=detail['signal_ref'] or reservation.digest!=record.payload_digest):raise ValueError('bot selection reservation truncated')
            selected={**detail,'selected_at_ns':event['at_ns'],'settlement':None,'settled_at_ns':None}
            if groups:
                group=db.get(GrvtMainnetBracket,detail['parent_client_order_id'])
                if group is not None:
                    from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
                    actual=verified_mainnet_bracket(db,group)
                    if actual['claim']['source']!='BOT_NATIVE_PROTECTED_ENTRY':raise ValueError('manual order used by bot')
                    receipt=actual['reconciliation']
                    if receipt is not None:
                        financial_outcomes.append({'parent_client_order_id':detail['parent_client_order_id'],
                            'receipt_digest':encode(receipt)[1],'reconciled_at_ns':receipt['reconciled_at_ns'],
                            'net_realized_pnl_usdt':receipt['result']['net_realized_pnl_usdt'],'bot_recorded':False})
            selections.append(selected)
        elif kind=='SETTLE':
            if (set(detail)!={'parent_client_order_id','receipt_digest','net_realized_pnl_usdt'} or not selections
                    or selections[-1]['settlement'] is not None or detail['parent_client_order_id']!=selections[-1]['parent_client_order_id']
                    or not re.fullmatch(r'[a-f0-9]{64}',detail['receipt_digest'])):raise ValueError('bot receipt link')
            number(detail['net_realized_pnl_usdt'])
            if groups:
                from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
                receipt=verified_mainnet_bracket(db,db.get(GrvtMainnetBracket,detail['parent_client_order_id']))['reconciliation']
                if (receipt is None or receipt['reconciled_at_ns']>event['at_ns'] or encode(receipt)[1]!=detail['receipt_digest']
                        or receipt['result']['net_realized_pnl_usdt']!=detail['net_realized_pnl_usdt']):
                    raise ValueError('bot financial receipt changed')
            selections[-1]['settlement']=detail;selections[-1]['settled_at_ns']=event['at_ns']
            for outcome in financial_outcomes:
                if outcome['parent_client_order_id']==detail['parent_client_order_id']:outcome['bot_recorded']=True
        elif kind=='AUTO_REARM':
            if (set(detail)!={'parent_client_order_id','stop_revision','released_revision','origin_digest'} or not selections
                    or selections[-1]['parent_client_order_id']!=detail['parent_client_order_id'] or selections[-1]['settlement'] is None
                    or type(detail['stop_revision']) is not int or detail['stop_revision']<1
                    or type(detail['released_revision']) is not int or detail['released_revision']!=detail['stop_revision']+1
                    or not re.fullmatch(r'[a-f0-9]{64}',detail['origin_digest'])):raise ValueError('unproven bot STOP release')
        elif kind=='FINISH':
            if (set(detail)!={'status'} or detail['status'] not in ('STOPPED','EXPIRED','COMPLETE','REVIEW','ENTRY_LIMIT','BUDGET_LIMIT','LOSS_LIMIT')
                    or detail['status'] in ('COMPLETE','ENTRY_LIMIT','BUDGET_LIMIT','LOSS_LIMIT') and any(item['settlement'] is None for item in selections)):
                raise ValueError('bot terminal')
            with localcontext() as context:
                context.prec=100
                net=sum((number(item['settlement']['net_realized_pnl_usdt']) for item in selections if item['settlement']),Decimal(0))
                if (detail['status']=='COMPLETE' and not selections
                        or detail['status']=='ENTRY_LIMIT' and len(selections)<policy.max_entries
                        or detail['status']=='BUDGET_LIMIT' and policy.entry_cap*(len(selections)+1)<=policy.total_entry_cap
                        or detail['status']=='LOSS_LIMIT' and -net<policy.loss_cap
                        or detail['status']=='EXPIRED' and event['at_ns']<value['entry_deadline_ns']):raise ValueError('unproven bot terminal reason')
            terminal=detail
        else:raise ValueError('unknown bot event')
        last=event['at_ns'];previous=record.payload_digest;seen.add(record.sequence)
    if (row.event_count!=len(seen) or row.head_digest!=previous or row.active_slot!=(policy.sub_account_id if terminal is None else None)):
        raise ValueError('bot event tail truncated')
    reserved=list(db.scalars(select(GrvtMainnetBotSelection.parent_client_order_id).where(GrvtMainnetBotSelection.run_id==row.id)))
    if set(reserved)!={item['parent_client_order_id'] for item in selections}:raise ValueError('bot reservation events truncated')
    return {**value,'authorization':False,'selections':selections,'terminal':terminal,'financial_outcomes':financial_outcomes}


def verify_all_bot_grants(db):
    bot_schema(db,required=True)
    for row in db.scalars(select(GrvtMainnetBotRun)):_read(db,row)


def read_bot_grant(vault,run_id):
    try:
        with vault.session() as db:
            bot_schema(db,required=True);return _read(db,db.get(GrvtMainnetBotRun,run_id))
    except GrvtMainnetError:raise
    except Exception:raise GrvtMainnetError('Concesión o evidencia del bot MAINNET no verificable') from None


def _append(db,row,kind,details,at):
    event={'run_id':row.id,'header_digest':row.payload_digest,'sequence':row.event_count+1,'previous_digest':row.head_digest,
        'at_ns':_stamp(at),'type':kind,'details':details}
    text,digest=encode(event)
    db.add(GrvtMainnetBotEvent(run_id=row.id,sequence=event['sequence'],payload_json=text,payload_digest=digest))
    row.event_count+=1;row.head_digest=digest
    return event,digest


def begin_bot_grant(vault,paper,lab,session_id,policy:MainnetBotPolicy,*,now_ns,keep_operating=None,run_id=None,research_binding=None):
    if not isinstance(policy,MainnetBotPolicy):raise GrvtMainnetError('Política del bot inválida')
    source=bot_source(paper,lab,session_id);start=_stamp(now_ns);run_id=run_id or str(uuid4())
    if str(UUID(run_id))!=run_id:raise GrvtMainnetError('Identificador de concesión inválido')
    value={'schema':'orbis.grvt.mainnet.bot.grant.v1','id':run_id,'network':'MAINNET','source':source,'policy':asdict(policy),
        'started_at_ns':start,'entry_deadline_ns':start+policy.wait_seconds*1_000_000_000,
        'deadline_ns':start+(policy.wait_seconds+policy.exit_seconds+policy.recovery_seconds)*1_000_000_000}
    if keep_operating is not None:
        if type(keep_operating) is not bool:raise GrvtMainnetError('Modo operativo inválido')
        value.update(schema='orbis.grvt.mainnet.bot.grant.v2',keep_operating=keep_operating)
    if research_binding is not None:
        from app.research.grvt_binding import read_grvt_research_binding
        research=read_grvt_research_binding(research_binding,source)
        value.update(schema='orbis.grvt.mainnet.bot.grant.v3',keep_operating=bool(keep_operating),research_binding=research.exported())
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE');verify_all_bot_grants(db)
            from app.live_execution.grvt_mainnet_journal import require_new_mainnet_bracket
            require_new_mainnet_bracket(db,policy.sub_account_id,str(2**63+5))
            if db.scalar(select(GrvtMainnetBotRun.id).where(GrvtMainnetBotRun.active_slot==policy.sub_account_id)) is not None:raise ValueError('bot already active')
            text,digest=encode(value);row=GrvtMainnetBotRun(id=run_id,sub_account_id=policy.sub_account_id,active_slot=policy.sub_account_id,
                payload_json=text,payload_digest=digest,event_count=0,head_digest=None)
            db.add(row);db.flush();_read(db,row);db.commit()
        except Exception:
            db.rollback();raise GrvtMainnetError('Subcuenta ocupada, detenida o permiso del bot no verificable') from None
    return read_bot_grant(vault,run_id)


def require_bot_submission(db,sub,parent,run_id,*,now_ns=None):
    if not bot_schema(db):
        if run_id is not None:raise GrvtMainnetError('Diario del bot MAINNET no disponible')
        return
    active=db.scalar(select(GrvtMainnetBotRun).where(GrvtMainnetBotRun.active_slot==sub))
    if active is None:
        if run_id is not None:raise GrvtMainnetError('Concesión del bot retirada')
        return
    if run_id!=active.id:raise GrvtMainnetError('Subcuenta reservada por el bot MAINNET')
    grant=_read(db,active,groups=False)
    from app.live_execution.grvt_perp_bot_grant import require_supervised_entry,SUPERVISED_SCHEMA
    require_supervised_entry(grant,now_ns)
    if not grant['selections'] or grant['selections'][-1]['parent_client_order_id']!=parent or grant['selections'][-1]['settlement'] is not None:
        raise GrvtMainnetError('Entrada no seleccionada por la concesión')
    if grant['schema']==SUPERVISED_SCHEMA and now_ns is not None:
        signal=grant['selections'][-1]['signal']
        if not 0<=now_ns-int(signal['source_close_ns'])<=15_000_000_000:
            raise GrvtMainnetError('Señal supervisada vencida antes del envío')


def verify_bot_claim(db,claim):
    bot_schema(db,required=True);parent=claim['parent_client_order_id'];reservation=db.get(GrvtMainnetBotSelection,parent)
    if reservation is None:raise ValueError('bot claim without selection')
    grant=_read(db,db.get(GrvtMainnetBotRun,reservation.run_id),groups=False)
    selected=next(item for item in grant['selections'] if item['parent_client_order_id']==parent)
    from app.live_execution.grvt_perp_bot_grant import SCHEMAS,CLAIM_SOURCE,SUPERVISED_SCHEMA
    if grant['schema'] in SCHEMAS:
        if (claim['schema']!='orbis.grvt.mainnet.bracket.claim.v3' or claim['source']!=CLAIM_SOURCE
                or claim['preflight'].get('plan')!=selected['plan']):
            raise ValueError('perpetual bot claim changed selected plan')
        if (grant['schema']==SUPERVISED_SCHEMA
                and not 0<=claim['claimed_at_ns']-int(selected['signal']['source_close_ns'])<=15_000_000_000):
            raise ValueError('supervised bot claim changed signal freshness')
    elif claim['schema']!='orbis.grvt.mainnet.bracket.claim.v1' or claim['source']!='BOT_NATIVE_PROTECTED_ENTRY':
        raise ValueError('legacy bot claim version changed')
    if (claim['draft']!=selected['draft'] or claim['instrument']!=selected['instrument']
            or not selected['selected_at_ns']<=claim['claimed_at_ns']<=grant['entry_deadline_ns']
            or claim['claimed_at_ns']-selected['recorded_at_ns']>SIGNAL_MAX_AGE_NS):raise ValueError('bot claim changed causal terms')


def _entry_draft(policy,source,instrument,quote,at):
    if (quote.get('instrument')!=instrument.name or not isinstance(quote.get('event_time'),str)
            or not 0<=at-int(quote['event_time'])<=5_000_000_000):raise GrvtMainnetError('Cotización del bot antigua')
    with localcontext() as context:
        context.prec=100;buy=source['definition']['direction']=='LONG';sign=1 if buy else -1;tick=instrument.tick_size
        price=(number(quote['best_ask_price' if buy else 'best_bid_price'])/tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)*tick
        size=(policy.entry_cap/price).quantize(Decimal(1).scaleb(-instrument.base_decimals),rounding=ROUND_FLOOR)
        sl=(price*(1-sign*Decimal(source['definition']['stop_loss']))/tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)*tick
        tp=(price*(1+sign*Decimal(source['definition']['take_profit']))/tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)*tick
    return MainnetBracketDraft(policy.sub_account_id,instrument.name,source['definition']['direction'],size,price,sl,tp,
        policy.entry_cap,policy.exit_cap,policy.exit_seconds,policy.max_spread_bps,policy.max_slippage_bps)


def submit_bot_entry_once(vault,paper,lab,run_id,*,client_factory,private_key,expected_signer,require_permission,public_post,clock_ns=time.time_ns):
    generation=vault.session_generation;require_permission();saved=read_bot_grant(vault,run_id);policy=_policy(saved['policy'])
    def checked():
        require_permission();at=_stamp(clock_ns())
        if generation is None or vault.session_generation!=generation or at>saved['entry_deadline_ns']:raise GrvtMainnetError('Permiso de entrada del bot vencido o retirado')
        return at
    def public(url,body):
        checked();reply=public_post(url,body);checked();return reply
    try:
        if saved['terminal'] is not None or bot_source(paper,lab,saved['source']['session_id'])!=saved['source']:raise ValueError('bot source changed')
        armed=max(saved['started_at_ns'],saved['selections'][-1]['settled_at_ns'] or 0) if saved['selections'] else saved['started_at_ns']
        candidate=next_bot_open(paper,saved['source'],armed_at_ns=armed,now_ns=checked())
        if candidate is None:raise ValueError('no future signal')
        action=candidate['action_request_id'];parent,reference=signal_parent(saved['source'],action)
        with reviewed_paper_signal(paper,lab,instrument=saved['source']['instrument'],session_id=saved['source']['session_id'],
                action_request_id=action,side='BUY' if saved['source']['definition']['direction']=='LONG' else 'SELL',
                reduce_only=False,now_ns=checked(),clock_ns=checked) as (binding,proof,definition,recorded,fresh):
            if definition!=saved['source']['definition'] or proof['dataset_hash']!=saved['source']['dataset_hash']:raise ValueError('different strategy')
            instrument=fetch_mainnet_instrument(saved['source']['instrument'],http_post=public)
            quote=_result(public(TICKER_URL,{'instrument':instrument.name}))['result'];draft=_entry_draft(policy,saved['source'],instrument,quote,fresh())
            unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=parent,nonces=(1,2,3),issued_at_ns=fresh())
            with vault.guard_session_generation(generation),vault.session() as db:
                try:
                    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row=db.get(GrvtMainnetBotRun,run_id);current=_read(db,row)
                    if current!=saved:raise ValueError('bot grant changed')
                    control=_control(db,policy.sub_account_id)
                    if control is not None and (control.stopped or control.pending_parent_id is not None):raise ValueError('subaccount stopped or pending')
                    detail={'action_request_id':action,'recorded_at_ns':recorded,'parent_client_order_id':parent,
                        'signal_ref':reference,'draft':asdict(draft),'instrument':asdict(instrument),
                        'quote':{key:quote[key] for key in ('instrument','event_time','best_bid_price','best_ask_price')}}
                    event,digest=_append(db,row,'SELECT',detail,fresh())
                    db.add(GrvtMainnetBotSelection(parent_client_order_id=parent,signal_ref=reference,run_id=run_id,sequence=event['sequence'],digest=digest))
                    db.flush();_read(db,row);fresh();db.commit()
                except Exception:db.rollback();raise
            from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
            return submit_mainnet_bracket_once(vault,draft,parent_client_order_id=parent,client_factory=client_factory,
                private_key=private_key,expected_signer=expected_signer,require_permission=lambda:fresh(),
                confirmation='ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET',public_post=public,clock_ns=fresh,bot_run_id=run_id)
    except GrvtMainnetError:raise
    except Exception:raise GrvtMainnetError('Señal o selección MAINNET no verificable; consulta el registro antes de otra acción') from None


def settle_bot_selection(vault,run_id,*,now_ns):
    """Asienta sólo un recibo financiero verificado; no concede otra entrada."""
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE');row=db.get(GrvtMainnetBotRun,run_id);saved=_read(db,row)
            if saved['terminal'] is not None or not saved['selections']:raise ValueError('bot not pending')
            selected=saved['selections'][-1]
            if selected['settlement'] is not None:return saved
            from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
            group=verified_mainnet_bracket(db,db.get(GrvtMainnetBracket,selected['parent_client_order_id']))
            receipt=group['reconciliation']
            if receipt is None:raise ValueError('bot group not financially reconciled')
            _append(db,row,'SETTLE',{'parent_client_order_id':selected['parent_client_order_id'],
                'receipt_digest':encode(receipt)[1],'net_realized_pnl_usdt':receipt['result']['net_realized_pnl_usdt']},now_ns)
            db.flush();result=_read(db,row);db.commit();return result
        except Exception:
            db.rollback();raise GrvtMainnetError('No se pudo asentar el recibo del bot MAINNET') from None


def finish_bot_grant(vault,run_id,*,status,now_ns):
    """Finaliza la reserva local y activa STOP; no cierra una posición remota."""
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE');row=db.get(GrvtMainnetBotRun,run_id);saved=_read(db,row)
            if saved['terminal'] is not None:
                if saved['terminal']['status']!=status:raise ValueError('different terminal')
                return saved
            _append(db,row,'FINISH',{'status':status},now_ns);row.active_slot=None
            control=_control(db,row.sub_account_id,create=True);was_stopped=control.stopped;control.stopped=True;control.revision+=1
            from app.live_execution.grvt_mainnet_stop_origin import record_stop
            record_stop(db,control,'BOT_TERMINAL',was_stopped=was_stopped)
            db.flush();result=_read(db,row);db.commit();return result
        except Exception:
            db.rollback();raise GrvtMainnetError('No se pudo verificar el final del bot MAINNET') from None
