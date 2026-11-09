"""Version-four REAL grant: independent rule feed and durable signal selection.

The existing subaccount reservation and encrypted event tables are shared with
legacy bots. No saved grant, replay cache or selection restores RAM authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select

from app.lab.store import encoded
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, unsigned_mainnet_bracket
from app.live_execution.grvt_mainnet_journal import _control, _draft, _instrument, encode
from app.live_execution.grvt_perp_bot_origin import PerpBotSource, VerifiedPerpBotOrigin
from app.live_execution.grvt_perp_plan import PerpEntryPlan, PerpEntryTerms, plan_draft
from app.live_execution.grvt_perp_signals import (
    MAX_CONSUMER_DELAY_NS, MAX_SIGNAL_DELAY_NS, PerpSignalSeed, PerpSignalState,
    signal_checkpoint, verify_signal_record,
)
from app.live_execution.grvt_perp_supervision import (
    SUPERVISED_SCHEMA,SUPERVISION_ENGINE,PerpSupervision,confirm_supervision_entry,entry_material,
    market_supervision,position_supervision,select_supervision,settle_supervision,settled_net,
    supervision_checkpoint,supervision_dependencies,visible_select,visible_settle,source_fault_supervision,
)
from app.live_execution.grvt_perp_position import PerpPositionReviewInput
from app.portfolio.models import (
    GrvtMainnetBotEvent, GrvtMainnetBotRun, GrvtMainnetBotSelection, GrvtMainnetBracket,
)


SCHEMA = 'orbis.grvt.mainnet.bot.grant.v4'
SCHEMAS = (SCHEMA,SUPERVISED_SCHEMA)
CLAIM_SOURCE = 'PERP_BOT_NATIVE_PROTECTED_ENTRY'
MAX_EVENTS = 100_000
MAX_MARKET_EVENTS = 50_000


def is_perp_grant(text: str) -> bool:
    return any('"schema":"'+schema+'"' in text for schema in SCHEMAS)


def _witness(row) -> tuple:
    return (row.run_id,row.sequence,row.payload_digest,hashlib.sha256(row.payload_json.encode()).hexdigest())


def _context(db,row) -> tuple:
    path = Path(db.get_bind().url.database)
    info = path.stat()
    return (info.st_dev,info.st_ino,row.id,row.sub_account_id,row.payload_digest,
            hashlib.sha256(row.payload_json.encode()).hexdigest())


@dataclass(frozen=True)
class _Prefix:
    context: tuple
    public_json: str
    initial: PerpSignalState
    state: PerpSignalState
    witnesses: tuple
    supervision: PerpSupervision|None=None
    entry_parents: tuple=()
    dependencies: tuple=()


def _terms_policy(terms,policy):
    from app.live_execution.grvt_mainnet_preflight import number
    budget = terms.budget
    if (policy.sub_account_id != budget.sub_account_id
            or policy.entry_cap != number(budget.entry_notional_limit)
            or policy.exit_cap != number(budget.exit_notional_limit)
            or any(getattr(policy,key) != getattr(budget,key) for key in
                ('exit_seconds','max_spread_bps','max_slippage_bps'))):
        raise ValueError('La política del bot no coincide con su presupuesto REAL')


def _header(row):
    from app.live_execution.grvt_mainnet_bot_entry import _policy, _stamp
    value = json.loads(row.payload_json)
    supervised=value.get('schema')==SUPERVISED_SCHEMA
    if (set(value) != {'schema','id','network','source','policy','terms','signal_seed',
            'started_at_ns','entry_deadline_ns','deadline_ns','keep_operating'}|({'supervision_engine'} if supervised else set())
            or value['schema'] not in SCHEMAS or value['network'] != 'MAINNET'
            or supervised and value['supervision_engine']!=SUPERVISION_ENGINE
            or type(value['keep_operating']) is not bool or str(UUID(value['id'])) != row.id
            or encode(value) != (row.payload_json,row.payload_digest)):
        raise ValueError('Integridad de la concesión perpetua REAL inválida')
    source = PerpBotSource.model_validate(value['source'])
    terms = PerpEntryTerms.model_validate(value['terms'])
    seed = PerpSignalSeed.model_validate(value['signal_seed'])
    policy = _policy(value['policy'])
    _terms_policy(terms,policy)
    start = _stamp(value['started_at_ns'])
    if (row.sub_account_id != policy.sub_account_id
            or not 0 <= start-int(seed.prepared_at_ns) <= MAX_CONSUMER_DELAY_NS
            or int(source.paper.last_received_at_ns) > start
            or terms.profile != source.research.profile or terms.definition != source.definition
            or terms.instrument != source.instrument or terms.timeframe != source.timeframe
            or seed.origin_reference_hash != source.research.sha256
            or seed.profile != terms.profile or seed.definition != terms.definition
            or seed.instrument != terms.instrument or seed.timeframe != terms.timeframe
            or _stamp(value['entry_deadline_ns']) != start+policy.wait_seconds*10**9
            or _stamp(value['deadline_ns']) != start+(policy.wait_seconds+policy.exit_seconds+policy.recovery_seconds)*10**9):
        raise ValueError('Alcance de la concesión perpetua REAL inválido')
    public = {key:item for key,item in value.items() if key != 'signal_seed'}
    public['grant_digest'] = row.payload_digest
    return public,seed.start()


def _signal_selection(source,run_id,decision):
    from app.live_execution.grvt_mainnet_bot_entry import signal_parent
    action = str(uuid5(UUID(run_id),decision['sha256']))
    parent,reference = signal_parent(source,action)
    return action,parent,reference


def _selection(detail, *, value, policy, latest, market_sequence, checkpoint, selections, at):
    from app.live_execution.grvt_mainnet_preflight import number
    if (set(detail) != {'action_request_id','recorded_at_ns','parent_client_order_id','signal_ref',
            'draft','instrument','signal','plan','market_sequence'}
            or latest is None or detail['signal'] != latest or not latest['entry_matched']
            or checkpoint['status'] != 'RUNNING' or detail['market_sequence'] != market_sequence
            or type(detail['market_sequence']) is not int
            or (detail['action_request_id'],detail['parent_client_order_id'],detail['signal_ref'])
                != _signal_selection(value['source'],value['id'],latest)
            or type(detail['recorded_at_ns']) is not int or detail['recorded_at_ns'] != int(latest['known_at_ns'])):
        raise ValueError('Selección de señal perpetua no reproducible')
    armed = max(value['started_at_ns'],selections[-1]['settled_at_ns'] or 0) if selections else value['started_at_ns']
    if (selections and selections[-1]['settlement'] is None
            or not armed < int(latest['source_close_ns']) <= detail['recorded_at_ns'] <= at <= value['entry_deadline_ns']
            or not 0 <= at-int(latest['source_close_ns']) <= MAX_SIGNAL_DELAY_NS
            or not 0 <= at-int(checkpoint['last_observed_ns']) <= MAX_CONSUMER_DELAY_NS
            or len(selections) >= policy.max_entries):
        raise ValueError('Señal perpetua anterior, caducada o con entrada pendiente')
    plan = PerpEntryPlan.model_validate(detail['plan'])
    if (plan.source.terms.payload() != value['terms'] or not 0 <= at-int(plan.source.checked_at_ns) <= MAX_CONSUMER_DELAY_NS
            or _draft(detail['draft']) != plan_draft(plan)
            or _instrument(detail['instrument']) != plan.source.instrument.instrument()):
        raise ValueError('La selección cambió su dimensionamiento REAL')
    with localcontext() as context:
        context.prec = 100
        if (policy.entry_cap*(len(selections)+1) > policy.total_entry_cap
                or -sum((number(item['settlement']['net_realized_pnl_usdt']) for item in selections),Decimal(0)) >= policy.loss_cap):
            raise ValueError('La selección supera límites totales o de pérdida')
    unsigned_mainnet_bracket(plan_draft(plan),plan.source.instrument.instrument(),
        parent_client_order_id=detail['parent_client_order_id'],nonces=(1,2,3),issued_at_ns=at)


def read_perp_bot_grant(db,row,*,groups=True,verify_full=False):
    """Cold replay or vault-owned prefix; actual prefix bytes are rechecked."""
    from app.live_execution.grvt_mainnet_bot_entry import _policy, _stamp
    if row is None:
        raise ValueError('Concesión perpetua REAL ausente')
    context = _context(db,row)
    items = list(db.scalars(select(GrvtMainnetBotEvent).where(GrvtMainnetBotEvent.run_id==row.id)
        .order_by(GrvtMainnetBotEvent.sequence)))
    if len(items) > MAX_EVENTS:
        raise ValueError('Diario de señales fuera de capacidad')
    cache = None if verify_full else db.info.get('orbis_grvt_perp_replay_cache')
    prefix = cache.get(row.id) if isinstance(cache,dict) else None
    witnesses = tuple(_witness(record) for record in items)
    if (isinstance(prefix,_Prefix) and prefix.context==context
            and len(witnesses)>=len(prefix.witnesses) and witnesses[:len(prefix.witnesses)]==prefix.witnesses
            and (prefix.supervision is None or prefix.dependencies==supervision_dependencies(db,prefix.entry_parents))):
        value = json.loads(prefix.public_json)
        initial,feed,offset = prefix.initial,prefix.state,len(prefix.witnesses)
        supervision=prefix.supervision
        entry_parents=list(prefix.entry_parents)
    else:
        value,initial = _header(row)
        feed,offset = initial,0
        supervision=PerpSupervision() if value['schema']==SUPERVISED_SCHEMA else None
        entry_parents=[]
    supervised=supervision is not None
    visible=supervision_checkpoint(PerpSupervision()) if supervised else None
    policy = _policy(value['policy'])
    checkpoint = signal_checkpoint(initial)
    latest,market_sequence = None,None
    selections,previous,last,terminal = [],None,value['started_at_ns'],None
    position_count=tick_count=fault_count=0
    for index,record in enumerate(items):
        event = json.loads(record.payload_json)
        if (set(event) != {'run_id','header_digest','sequence','previous_digest','at_ns','type','details'}
                or event['run_id'] != row.id or event['header_digest'] != row.payload_digest
                or type(event['sequence']) is not int or event['sequence'] != index+1 or record.sequence != index+1
                or event['previous_digest'] != previous or encode(event) != (record.payload_json,record.payload_digest)
                or _stamp(event['at_ns']) < last or terminal is not None):
            raise ValueError('Cadena de eventos perpetuos REAL inválida')
        detail,kind,at = event['details'],event['type'],event['at_ns']
        if kind == 'MARKET':
            if supervised:
                if type(detail) is not dict or set(detail)!={'market','supervision'}:
                    raise ValueError('Mercado supervisado inválido')
                market=detail['market']
            else:market=detail
            if index >= offset:
                feed = verify_signal_record(feed,market).state
                if supervised:
                    supervision=market_supervision(supervision,feed,now_ns=at)
                    if encoded(detail['supervision'])!=encoded(supervision_checkpoint(supervision)):
                        raise ValueError('Salida supervisada de mercado no reproducible')
            checkpoint = market['checkpoint']
            if supervised:visible=detail['supervision']
            if (not value['started_at_ns'] <= int(checkpoint['last_accepted_ns']) <= at <= value['deadline_ns']
                    or checkpoint['sequence'] > MAX_MARKET_EVENTS
                    or at-int(checkpoint['last_accepted_ns']) > MAX_CONSUMER_DELAY_NS):
                raise ValueError('Escritura del mercado perpetuo futura o atrasada')
            for decision in market['decisions']:
                if decision['entry_matched']:
                    latest,market_sequence = decision,record.sequence
        elif kind == 'SELECT':
            if supervised and visible['halt_new_entries']:
                raise ValueError('La supervisión impide nuevas entradas')
            _selection(detail,value=value,policy=policy,latest=latest,market_sequence=market_sequence,
                checkpoint=checkpoint,selections=selections,at=at)
            reservation = db.get(GrvtMainnetBotSelection,detail['parent_client_order_id'])
            if (reservation is None or reservation.run_id != row.id or reservation.sequence != record.sequence
                    or reservation.signal_ref != detail['signal_ref'] or reservation.digest != record.payload_digest):
                raise ValueError('Reserva de señal perpetua ausente o alterada')
            selections.append({**detail,'selected_at_ns':at,'settlement':None,'settled_at_ns':None})
            if supervised:
                if index>=offset:supervision=select_supervision(supervision,detail['parent_client_order_id'])
                visible=visible_select(visible,detail['parent_client_order_id'])
        elif supervised and kind in ('ENTRY_CONFIRMED','POSITION_REVIEW','SUPERVISION_TICK','SOURCE_FAULT'):
            if at>value['deadline_ns']:
                raise ValueError('Supervisión posterior al permiso')
            if kind=='ENTRY_CONFIRMED':
                if (set(detail)!={'parent_client_order_id','entry_digest','supervision'} or not selections
                        or selections[-1]['settlement'] is not None
                        or detail['parent_client_order_id']!=selections[-1]['parent_client_order_id']):
                    raise ValueError('Confirmación supervisada sin selección')
                parent=detail['parent_client_order_id']
                if parent in entry_parents and index>=offset:
                    raise ValueError('Entrada supervisada duplicada')
                if index>=offset:
                    entry,claim=entry_material(db,parent,detail['entry_digest'],selections[-1])
                    supervision=confirm_supervision_entry(supervision,feed,entry,claim,
                        PerpEntryTerms.model_validate(value['terms']),now_ns=at)
                if parent not in entry_parents:entry_parents.append(parent)
            elif kind=='POSITION_REVIEW':
                position_count+=1
                if (set(detail)!={'parent_client_order_id','input','result','supervision'} or not selections
                        or detail['parent_client_order_id']!=visible['parent_client_order_id'] or position_count>20000):
                    raise ValueError('Vigilancia de posición inválida o fuera de capacidad')
                if index>=offset:
                    supervision,result=position_supervision(supervision,PerpPositionReviewInput.model_validate(detail['input']),
                        PerpEntryTerms.model_validate(value['terms']),policy,settled_net=settled_net(selections),now_ns=at)
                    if encoded(result)!=encoded(detail['result']):
                        raise ValueError('Vigilancia de posición no reproducible')
            elif kind=='SOURCE_FAULT':
                fault_count+=1
                if set(detail)!={'reason','supervision'} or fault_count>1:
                    raise ValueError('Fallo de fuente duplicado o inválido')
                if index>=offset:supervision=source_fault_supervision(supervision,detail['reason'])
            else:
                tick_count+=1
                if set(detail)!={'supervision'} or tick_count>5000:
                    raise ValueError('Control de vigilancia inválido o fuera de capacidad')
                if index>=offset:supervision=market_supervision(supervision,feed,now_ns=at)
            if index>=offset and encoded(detail['supervision'])!=encoded(supervision_checkpoint(supervision)):
                raise ValueError('Checkpoint supervisado no reproducible')
            visible=detail['supervision']
        elif kind == 'SETTLE':
            if (set(detail) != {'parent_client_order_id','receipt_digest','net_realized_pnl_usdt'} or not selections
                    or selections[-1]['settlement'] is not None or detail['parent_client_order_id'] != selections[-1]['parent_client_order_id']
                    or type(detail['receipt_digest']) is not str or not re.fullmatch('[a-f0-9]{64}',detail['receipt_digest'])):
                raise ValueError('Enlace financiero perpetuo inválido')
            from app.live_execution.grvt_mainnet_preflight import number
            number(detail['net_realized_pnl_usdt'])
            selections[-1]['settlement'],selections[-1]['settled_at_ns'] = detail,at
            if supervised:
                if index>=offset:supervision=settle_supervision(supervision,detail['parent_client_order_id'])
                visible=visible_settle(visible)
        elif kind == 'AUTO_REARM':
            if supervised and visible['halt_new_entries']:
                raise ValueError('La supervisión exige nueva autorización')
            if (set(detail) != {'parent_client_order_id','stop_revision','released_revision','origin_digest'} or not selections
                    or selections[-1]['settlement'] is None or detail['parent_client_order_id'] != selections[-1]['parent_client_order_id']
                    or type(detail['stop_revision']) is not int or detail['stop_revision'] < 1
                    or type(detail['released_revision']) is not int or detail['released_revision'] != detail['stop_revision']+1
                    or type(detail['origin_digest']) is not str or not re.fullmatch('[a-f0-9]{64}',detail['origin_digest'])
                    or at>value['deadline_ns']):
                raise ValueError('Rearme perpetuo sin origen de STOP')
        elif kind == 'FINISH':
            if set(detail) != {'status'} or detail['status'] not in (
                    'STOPPED','EXPIRED','COMPLETE','REVIEW','ENTRY_LIMIT','BUDGET_LIMIT','LOSS_LIMIT'):
                raise ValueError('Final perpetuo inválido')
            with localcontext() as context_decimal:
                context_decimal.prec=100
                net=sum((Decimal(item['settlement']['net_realized_pnl_usdt']) for item in selections if item['settlement']),Decimal(0))
                if (detail['status'] in ('COMPLETE','ENTRY_LIMIT','BUDGET_LIMIT','LOSS_LIMIT') and
                        (not selections or any(item['settlement'] is None for item in selections))
                        or detail['status']=='ENTRY_LIMIT' and len(selections)<policy.max_entries
                        or detail['status']=='BUDGET_LIMIT' and policy.entry_cap*(len(selections)+1)<=policy.total_entry_cap
                        or detail['status']=='LOSS_LIMIT' and -net<policy.loss_cap
                        or detail['status']=='EXPIRED' and at<value['entry_deadline_ns']):
                    raise ValueError('Motivo final perpetuo no acreditado')
            terminal=detail
        else:
            raise ValueError('Evento perpetuo desconocido')
        previous,last = record.payload_digest,at
    if (row.event_count != len(items) or row.head_digest != previous
            or row.active_slot != (policy.sub_account_id if terminal is None else None)):
        raise ValueError('Cola de la concesión perpetua truncada')
    reserved = set(db.scalars(select(GrvtMainnetBotSelection.parent_client_order_id)
        .where(GrvtMainnetBotSelection.run_id==row.id)))
    if reserved != {item['parent_client_order_id'] for item in selections}:
        raise ValueError('Reservas perpetuas sin evento')
    outcomes=[]
    if groups:
        from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
        for selected in selections:
            group = db.get(GrvtMainnetBracket,selected['parent_client_order_id'])
            actual = verified_mainnet_bracket(db,group) if group is not None else None
            if actual is not None and actual['claim']['source'] != CLAIM_SOURCE:
                raise ValueError('Entrada ajena utilizada por el bot perpetuo')
            receipt = actual['reconciliation'] if actual is not None else None
            if receipt is not None:
                outcomes.append({'parent_client_order_id':selected['parent_client_order_id'],
                    'receipt_digest':encode(receipt)[1],'reconciled_at_ns':receipt['reconciled_at_ns'],
                    'net_realized_pnl_usdt':receipt['result']['net_realized_pnl_usdt'],
                    'bot_recorded':selected['settlement'] is not None})
            if selected['settlement'] is not None:
                if (receipt is None or receipt['reconciled_at_ns']>selected['settled_at_ns']
                        or encode(receipt)[1] != selected['settlement']['receipt_digest']
                        or receipt['result']['net_realized_pnl_usdt'] != selected['settlement']['net_realized_pnl_usdt']):
                    raise ValueError('Recibo financiero perpetuo alterado')
    if isinstance(cache,dict):
        cache.pop(row.id,None)
        while len(cache)>=2:
            cache.pop(next(iter(cache)))
        cache[row.id]=_Prefix(context,encoded(value),initial,feed,witnesses,supervision,tuple(entry_parents),
            supervision_dependencies(db,tuple(entry_parents)) if supervised else ())
    return {**value,'authorization':False,'signal_checkpoint':checkpoint,'latest_signal':deepcopy(latest),
        'latest_signal_market_sequence':market_sequence,'selections':selections,'terminal':terminal,'financial_outcomes':outcomes,
        **({'supervision':visible} if supervised else {})}


def begin_perp_bot_grant(vault,origin: VerifiedPerpBotOrigin,seed: PerpSignalSeed,terms: PerpEntryTerms,
                         policy,*,now_ns,keep_operating=False,run_id=None,supervised=False):
    from app.live_execution.grvt_mainnet_bot_entry import verify_all_bot_grants, _stamp
    from app.live_execution.grvt_mainnet_journal import require_new_mainnet_bracket
    if not isinstance(origin,VerifiedPerpBotOrigin):
        raise GrvtMainnetError('Falta el origen LAB/PAPER verificado para REAL')
    if type(supervised) is not bool:
        raise GrvtMainnetError('Modo de supervisión inválido')
    generation=vault.session_generation
    seed=PerpSignalSeed.model_validate(seed.payload())
    prefix=json.loads(origin.origin.trade_prefix_json)
    if [bar.model_dump() for bar in seed.history[:len(prefix)]] != prefix:
        raise GrvtMainnetError('El seed REAL no conserva el prefijo LAB original')
    source=origin.source()
    value={'schema':SCHEMA,'id':run_id or str(uuid4()),'network':'MAINNET','source':source.payload(),
        'policy':asdict(policy),'terms':terms.payload(),'signal_seed':seed.payload(),'keep_operating':keep_operating,
        'started_at_ns':_stamp(now_ns),'entry_deadline_ns':now_ns+policy.wait_seconds*10**9,
        'deadline_ns':now_ns+(policy.wait_seconds+policy.exit_seconds+policy.recovery_seconds)*10**9}
    if supervised:value.update(schema=SUPERVISED_SCHEMA,supervision_engine=SUPERVISION_ENGINE)
    with origin.hold_current(),vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            verify_all_bot_grants(db)
            require_new_mainnet_bracket(db,policy.sub_account_id,str(2**63+559))
            if db.scalar(select(GrvtMainnetBotRun.id).where(GrvtMainnetBotRun.active_slot==policy.sub_account_id)) is not None:
                raise ValueError('Subcuenta ocupada')
            text,digest=encode(value)
            row=GrvtMainnetBotRun(id=value['id'],sub_account_id=policy.sub_account_id,active_slot=policy.sub_account_id,
                payload_json=text,payload_digest=digest,event_count=0,head_digest=None)
            db.add(row);db.flush()
            result=read_perp_bot_grant(db,row)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise GrvtMainnetError('No se pudo reservar la concesión perpetua REAL') from None


def append_perp_market(vault,run_id,record,*,require_permission,clock_ns):
    from app.live_execution.grvt_mainnet_bot_entry import _append
    generation=vault.session_generation
    require_permission()
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row=db.get(GrvtMainnetBotRun,run_id)
            saved=read_perp_bot_grant(db,row,groups=False)
            at=clock_ns()
            if saved['terminal'] is not None:
                raise ValueError('Concesión cerrada')
            if saved['schema']==SUPERVISED_SCHEMA:
                prefix=db.info.get('orbis_grvt_perp_replay_cache',{}).get(run_id)
                if not isinstance(prefix,_Prefix) or prefix.supervision is None:
                    raise ValueError('Falta el cursor privado supervisado')
                updated=verify_signal_record(prefix.state,record).state
                supervised=market_supervision(prefix.supervision,updated,now_ns=at)
                record={'market':record,'supervision':supervision_checkpoint(supervised)}
            require_permission()
            _append(db,row,'MARKET',record,at)
            db.flush()
            result=read_perp_bot_grant(db,row,groups=False)
            require_permission()
            db.commit()
            return result['signal_checkpoint']
        except Exception:
            db.rollback()
            raise GrvtMainnetError('El evento de mercado no quedó confirmado; no reintentes la escritura') from None


@dataclass(frozen=True)
class PerpBotSelectionRequest:
    run_id: str
    signal_hash: str

    def __post_init__(self):
        try:
            if (type(self.run_id) is not str or str(UUID(self.run_id)) != self.run_id
                    or type(self.signal_hash) is not str or not re.fullmatch('[a-f0-9]{64}',self.signal_hash)):
                raise ValueError('selection scope')
        except (ValueError,TypeError,AttributeError):
            raise GrvtMainnetError('Identidad de la selección perpetua inválida') from None


def check_perp_candidate(db,request,terms,parent,at):
    row=db.get(GrvtMainnetBotRun,request.run_id)
    if row is None:
        raise GrvtMainnetError('Concesión perpetua no encontrada')
    saved=read_perp_bot_grant(db,row)
    if saved['schema']==SUPERVISED_SCHEMA:
        require_supervised_entry(saved,at)
    signal=saved['latest_signal']
    if (saved['terminal'] is not None or saved['terms'] != terms.payload() or signal is None
            or signal['sha256'] != request.signal_hash
            or parent != _signal_selection(saved['source'],saved['id'],signal)[1]):
        raise GrvtMainnetError('La señal no pertenece a esta concesión perpetua')
    control=_control(db,terms.budget.sub_account_id)
    if control is not None and (control.stopped or control.pending_parent_id is not None):
        raise GrvtMainnetError('Subcuenta detenida o con entrada pendiente')
    if db.get(GrvtMainnetBracket,parent) is not None or db.get(GrvtMainnetBotSelection,parent) is not None:
        raise GrvtMainnetError('La señal ya fue seleccionada; consulta su registro')
    # The full selection also checks the independently collected plan.
    if not 0 <= at-int(signal['source_close_ns']) <= MAX_SIGNAL_DELAY_NS:
        raise GrvtMainnetError('Señal perpetua caducada')
    return saved


def require_supervised_entry(saved,now_ns=None):
    if saved['schema']!=SUPERVISED_SCHEMA:
        return
    if saved['terminal'] is not None or saved['supervision']['halt_new_entries']:
        raise GrvtMainnetError('La vigilancia del bot impide nuevas entradas')
    if now_ns is not None:
        from app.live_execution.grvt_mainnet_bot_entry import _stamp
        _stamp(now_ns)
        checkpoint=saved['signal_checkpoint']
        if (checkpoint['status']!='RUNNING' or now_ns>saved['entry_deadline_ns']
                or any(not 0<=now_ns-int(checkpoint[key])<=MAX_CONSUMER_DELAY_NS
                       for key in ('last_observed_ns','quote_time_ns'))):
            raise GrvtMainnetError('Mercado supervisado antiguo, interrumpido o vencido')


def append_supervision(vault,run_id,kind,*,parent=None,entry_digest=None,position_input=None,source_fault=None,
                       require_permission,clock_ns):
    """Append validated safety evidence; never changes a remote position."""
    from app.live_execution.grvt_mainnet_bot_entry import _append,_policy
    generation=vault.session_generation
    require_permission()
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row=db.get(GrvtMainnetBotRun,run_id)
            saved=read_perp_bot_grant(db,row)
            prefix=db.info.get('orbis_grvt_perp_replay_cache',{}).get(run_id)
            if (saved['schema']!=SUPERVISED_SCHEMA or saved['terminal'] is not None
                    or not isinstance(prefix,_Prefix) or prefix.supervision is None):
                raise ValueError('Concesión sin supervisión activa')
            at=clock_ns()
            terms=PerpEntryTerms.model_validate(saved['terms'])
            if kind=='ENTRY_CONFIRMED':
                if not saved['selections'] or saved['selections'][-1]['settlement'] is not None:
                    raise ValueError('Confirmación sin entrada pendiente')
                entry,claim=entry_material(db,parent,entry_digest,saved['selections'][-1])
                updated=confirm_supervision_entry(prefix.supervision,prefix.state,entry,claim,terms,now_ns=at)
                detail={'parent_client_order_id':parent,'entry_digest':entry_digest,
                        'supervision':supervision_checkpoint(updated)}
            elif kind=='POSITION_REVIEW':
                if parent!=prefix.supervision.parent or not isinstance(position_input,PerpPositionReviewInput):
                    raise ValueError('Vigilancia de otra posición')
                source=PerpPositionReviewInput.model_validate(position_input.payload())
                updated,result=position_supervision(prefix.supervision,source,terms,_policy(saved['policy']),
                    settled_net=settled_net(saved['selections']),now_ns=at)
                detail={'parent_client_order_id':parent,'input':source.payload(),'result':result,
                        'supervision':supervision_checkpoint(updated)}
            elif kind=='SUPERVISION_TICK':
                updated=market_supervision(prefix.supervision,prefix.state,now_ns=at)
                detail={'supervision':supervision_checkpoint(updated)}
            elif kind=='SOURCE_FAULT':
                updated=source_fault_supervision(prefix.supervision,source_fault)
                detail={'reason':source_fault,'supervision':supervision_checkpoint(updated)}
            else:
                raise ValueError('Evento de supervisión no disponible')
            require_permission()
            _append(db,row,kind,detail,at)
            db.flush()
            result=read_perp_bot_grant(db,row)
            require_permission()
            db.commit()
            return result['supervision']
        except Exception:
            db.rollback()
            raise GrvtMainnetError('Supervisión no confirmada; consulta el registro antes de otra acción') from None


def select_perp_entry(vault,request,plan,parent,*,require_permission,clock_ns):
    from app.live_execution.grvt_mainnet_bot_entry import _append, _policy
    generation=vault.session_generation
    require_permission()
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            at=clock_ns()
            saved=check_perp_candidate(db,request,plan.source.terms,parent,at)
            signal=saved['latest_signal']
            action,_,reference=_signal_selection(saved['source'],saved['id'],signal)
            detail={'action_request_id':action,'recorded_at_ns':int(signal['known_at_ns']),
                'parent_client_order_id':parent,'signal_ref':reference,'signal':signal,'plan':plan.payload(),
                'draft':asdict(plan_draft(plan)),'instrument':asdict(plan.source.instrument.instrument()),
                'market_sequence':saved['latest_signal_market_sequence']}
            _selection(detail,value=saved,policy=_policy(saved['policy']),latest=signal,
                market_sequence=saved['latest_signal_market_sequence'],checkpoint=saved['signal_checkpoint'],
                selections=saved['selections'],at=at)
            row=db.get(GrvtMainnetBotRun,request.run_id)
            event,digest=_append(db,row,'SELECT',detail,at)
            db.add(GrvtMainnetBotSelection(parent_client_order_id=parent,signal_ref=reference,
                run_id=request.run_id,sequence=event['sequence'],digest=digest))
            db.flush()
            read_perp_bot_grant(db,row)
            require_permission()
            db.commit()
        except Exception:
            db.rollback()
            raise GrvtMainnetError('Selección perpetua no confirmada; consulta antes de otra acción') from None
