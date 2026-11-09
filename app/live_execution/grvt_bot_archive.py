"""Episodio automático TESTNET cifrado: permiso, selección única y resultado.

La reserva de subcuenta sobrevive al reinicio para impedir otro envío ciego.
Una lectura nunca restaura autoridad. El operador puede detener explícitamente
un episodio interrumpido y recuperar sus tres IDs en el diario de órdenes.
"""

from decimal import Decimal
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import select

from app.live_execution.grvt_bot_source import verify_bot_source as _verified_source
from app.lab.engine import CURRENT_ENGINE_VERSION, INTERVALS, validate_strategy, validate_rule_timeframes
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_preview import MAX_ENTRY_NOTIONAL, MAX_EXIT_NOTIONAL
from app.live_execution.grvt_testnet_oco import _encode, _stamp
from app.live_execution.grvt_testnet_preflight import client_id_for_signal
from app.live_execution.grvt_testnet_signal import SIGNAL_MAX_AGE_NS
from app.marketdata.adapters.grvt import GrvtAdapter
from app.portfolio.models import GrvtTestnetBotRun, GrvtTestnetStop, GrvtTestnetBracketGroup, GrvtTestnetBracketStrategy


STATUSES = {'STOPPED', 'EXPIRED', 'COMPLETE', 'NEEDS_REVIEW', 'GAP', 'ATTEMPT_LIMIT', 'REVIEW_UNSENT',
    'ENTRY_LIMIT', 'BUDGET_LIMIT', 'LOSS_LIMIT'}
CAUSES = {'NONE', 'USER_STOP', 'VAULT_LOCKED', 'VIEW_PROTECTED', 'SHUTDOWN', 'REVOKED'}
_SOURCE_KEYS = {'session_id','account_id','version_id','dataset_id','dataset_hash','symbol','instrument',
    'timeframe','engine_version','session_started_at_ns','definition'}
_HEADER_KEYS = {'schema','id','network','sub_account_id','source','started_at_ns','entry_deadline_ns',
    'entry_cap','exit_cap','exit_seconds','max_attempts','max_entries','max_spread_bps','max_slippage_bps'}


def _header(row):
    value = json.loads(row.payload_json)
    bounded = value.get('schema') == 'orbis.grvt.testnet.bot.run.v2'
    if (set(value) != _HEADER_KEYS | ({'total_entry_cap','loss_cap'} if bounded else set())
            or value['schema'] not in ('orbis.grvt.testnet.bot.run.v1','orbis.grvt.testnet.bot.run.v2')
            or value['network'] != 'TESTNET' or str(UUID(value['id'])) != row.id or value['id'] != row.id
            or value['sub_account_id'] != row.sub_account_id
            or not re.fullmatch(r'[0-9]{1,30}', row.sub_account_id)
            or str(value['started_at_ns']) != row.started_at_ns
            or _encode(value) != (row.payload_json, row.payload_digest)):
        raise ValueError('bot identity')
    _verified_source(value['source'])
    for key in ('started_at_ns','entry_deadline_ns'): _stamp(value[key])
    if not 30_000_000_000 <= value['entry_deadline_ns']-value['started_at_ns'] <= 86400_000_000_000:
        raise ValueError('entry deadline')
    if value['source']['session_started_at_ns'] > value['started_at_ns']: raise ValueError('source from future')
    for key, maximum in (('entry_cap', MAX_ENTRY_NOTIONAL), ('exit_cap', MAX_EXIT_NOTIONAL)):
        if (not isinstance(value[key], str) or not re.fullmatch(r'[0-9]{1,18}(\.[0-9]{1,18})?', value[key])
                or not 0 < Decimal(value[key]) <= maximum): raise ValueError('notional cap')
    for key, low, high in (('exit_seconds',30,86280), ('max_attempts',1,3), ('max_entries',2 if bounded else 1,20 if bounded else 1),
            ('max_spread_bps',0,100), ('max_slippage_bps',0,100)):
        if type(value[key]) is not int or not low <= value[key] <= high: raise ValueError('bot limits')
    if bounded:
        for key in ('total_entry_cap','loss_cap'):
            if (not isinstance(value[key],str) or not re.fullmatch(r'[0-9]{1,18}(\.[0-9]{1,18})?',value[key])
                    or not 0 < Decimal(value[key]) <= Decimal('1000')): raise ValueError('aggregate limit')
        if (Decimal(value['entry_cap']) > Decimal(value['total_entry_cap'])
                or Decimal(value['loss_cap']) > Decimal(value['total_entry_cap'])
                or value['entry_deadline_ns']-value['started_at_ns']+value['exit_seconds']*1_000_000_000 > 86400_000_000_000):
            raise ValueError('aggregate scope')
    return value


def _read(db, row):
    header = _header(row)
    if header['schema'] == 'orbis.grvt.testnet.bot.run.v2':
        from app.live_execution.grvt_bot_cycles import read_bounded_run
        result=read_bounded_run(db,row,header)
    else: result=_read_single(db,row)
    execution=None
    if result['selection'] is not None:
        from app.live_execution.grvt_testnet_bracket_journal import _verified
        from app.live_execution.grvt_testnet_bracket_finance import verified_bracket_reconciliation
        group=db.get(GrvtTestnetBracketGroup,result['selection']['parent_client_order_id'])
        if group is not None:
            plan=_verified(db,group); receipt=verified_bracket_reconciliation(db,group,plan)
            if receipt is not None:
                filled=receipt['result']['entry_filled_size']
                status='UNFILLED' if Decimal(filled)==0 else 'FILLED' if Decimal(filled)==Decimal(plan['legs'][0]['size']) else 'PARTIAL'
                execution={'status':status,'filled_size':filled,'basis':'RECONCILED'}
    return {**result,'entry_execution':execution}


def check_bot_selection_group(db, header, selection):
    group = db.get(GrvtTestnetBracketGroup, selection['parent_client_order_id'])
    if group is not None:
        from app.live_execution.grvt_testnet_bracket_journal import _verified
        from app.live_execution.grvt_bracket_strategy_snapshot import verified_bracket_strategy
        plan = _verified(db, group); source = header['source']; origin = plan['signal_source']
        if (group.sub_account_id != header['sub_account_id'] or plan['instrument'] != source['instrument']
                or any(origin[key] != source[key] for key in ('session_id','version_id','dataset_id','dataset_hash'))
                or origin['action_request_id'] != selection['action_request_id'] or origin['action'] != 'OPEN'
                or Decimal(plan['entry_cap']) != Decimal(header['entry_cap'])
                or Decimal(plan['exit_cap']) != Decimal(header['exit_cap'])):
            raise ValueError('different bot order')
        strategy = db.get(GrvtTestnetBracketStrategy, group.parent_client_order_id)
        if strategy is not None:
            frozen = verified_bracket_strategy(db, strategy)
            if any(frozen[key] != source[key] for key in ('definition','timeframe','engine_version')):
                raise ValueError('different bot strategy')


def _read_single(db, row):
    header = _header(row)
    selection = None; terminal = None
    if row.selection_json is not None:
        selection = json.loads(row.selection_json)
        if (set(selection) != {'run_id','header_digest','action_request_id','parent_client_order_id',
                'recorded_at_ns','selected_at_ns'} or selection['run_id'] != row.id
                or selection['header_digest'] != row.payload_digest
                or _encode(selection) != (row.selection_json, row.selection_digest)
                or str(UUID(selection['action_request_id'])) != selection['action_request_id']
                or client_id_for_signal(header['source']['session_id'], selection['action_request_id'])
                    != selection['parent_client_order_id']):
            raise ValueError('bot selection')
        for key in ('recorded_at_ns','selected_at_ns'): _stamp(selection[key])
        if (not header['started_at_ns'] < selection['recorded_at_ns'] <= selection['selected_at_ns'] <= header['entry_deadline_ns']
                or selection['selected_at_ns']-selection['recorded_at_ns'] > SIGNAL_MAX_AGE_NS):
            raise ValueError('old bot signal')
        check_bot_selection_group(db,header,selection)
    elif row.selection_digest is not None:
        raise ValueError('selection truncated')
    if row.terminal_json is not None:
        terminal = json.loads(row.terminal_json)
        if (set(terminal) != {'run_id','header_digest','selection_digest','at_ns','status','cause'}
                or terminal['run_id'] != row.id or terminal['header_digest'] != row.payload_digest
                or terminal['selection_digest'] != row.selection_digest
                or terminal['status'] not in STATUSES or terminal['cause'] not in CAUSES
                or _encode(terminal) != (row.terminal_json,row.terminal_digest)):
            raise ValueError('bot terminal')
        _stamp(terminal['at_ns'])
        if terminal['at_ns'] < (selection['selected_at_ns'] if selection else header['started_at_ns']):
            raise ValueError('regressed terminal')
        if terminal['status'] == 'COMPLETE':
            if selection is None: raise ValueError('complete without order')
            from app.live_execution.grvt_exit_watch_archive import _require_flat_receipt
            _require_flat_receipt(db, selection['parent_client_order_id'], terminal['at_ns'])
        elif terminal['status'] in ('ENTRY_LIMIT','BUDGET_LIMIT','LOSS_LIMIT'):
            raise ValueError('aggregate status on a single episode')
    elif row.terminal_digest is not None:
        raise ValueError('terminal truncated')
    if row.active_slot != (None if terminal else row.sub_account_id): raise ValueError('bot reservation changed')
    return {**header, 'selection': selection, 'terminal': terminal,
        'status': terminal['status'] if terminal else 'INTERRUPTED', 'authorization': False}


def begin_bot_run(vault, source: dict, *, sub_account_id: str, entry_cap: Decimal, exit_cap: Decimal,
        wait_seconds: int, exit_seconds: int, max_attempts: int, max_spread_bps: int,
        max_slippage_bps: int, now_ns: int, max_entries: int = 1, total_entry_cap: Decimal | None = None,
        loss_cap: Decimal | None = None) -> str:
    try:
        if type(wait_seconds) is not int or not 30 <= wait_seconds <= 86400: raise ValueError('wait limit')
        if any(type(cap) is not Decimal or not cap.is_finite() for cap in (entry_cap, exit_cap)):
            raise ValueError('decimal caps required')
        if type(max_entries) is not int or not 1 <= max_entries <= 20: raise ValueError('entry limit')
        if max_entries == 1 and (total_entry_cap is not None or loss_cap is not None): raise ValueError('single episode scope')
        with vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            stop = db.get(GrvtTestnetStop, sub_account_id)
            if stop and stop.stopped: raise ValueError('subaccount stopped')
            run = str(uuid4())
            payload = {'schema':'orbis.grvt.testnet.bot.run.v1', 'id':run, 'network':'TESTNET',
                'sub_account_id':sub_account_id, 'source':source, 'started_at_ns':now_ns,
                'entry_deadline_ns':now_ns+wait_seconds*1_000_000_000,
                'entry_cap':format(entry_cap,'f'), 'exit_cap':format(exit_cap,'f'),
                'exit_seconds':exit_seconds, 'max_attempts':max_attempts, 'max_entries':max_entries,
                'max_spread_bps':max_spread_bps, 'max_slippage_bps':max_slippage_bps}
            if max_entries > 1:
                if any(type(cap) is not Decimal or not cap.is_finite() for cap in (total_entry_cap,loss_cap)):
                    raise ValueError('explicit aggregate decimal limits required')
                payload.update(schema='orbis.grvt.testnet.bot.run.v2',total_entry_cap=format(total_entry_cap,'f'),loss_cap=format(loss_cap,'f'))
            encoded, digest = _encode(payload)
            row = GrvtTestnetBotRun(id=run, sub_account_id=sub_account_id, active_slot=sub_account_id,
                started_at_ns=str(now_ns), payload_json=encoded, payload_digest=digest)
            db.add(row); db.flush(); _read(db,row); db.commit(); return run
    except Exception:
        raise GrvtTestnetBracketError('No se pudo reservar el ciclo; revisa parada e historial de la subcuenta') from None


def select_bot_open(vault, run_id: str, candidate: dict, *, now_ns: int) -> None:
    try:
        with vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = db.get(GrvtTestnetBotRun, run_id); saved = _read(db,row)
            if saved['schema'] == 'orbis.grvt.testnet.bot.run.v2':
                from app.live_execution.grvt_bot_cycles import select_bounded_open
                select_bounded_open(db,row,saved,candidate,now_ns); db.commit(); return
            if saved['selection'] or saved['terminal']: raise ValueError('episode consumed')
            payload = {'run_id':run_id, 'header_digest':row.payload_digest, **candidate, 'selected_at_ns':now_ns}
            row.selection_json, row.selection_digest = _encode(payload)
            db.flush(); _read(db,row); db.commit()
    except Exception:
        raise GrvtTestnetBracketError('La señal del ciclo no se pudo reservar una sola vez') from None


def finish_bot_run(vault, run_id: str, *, status: str, cause: str, now_ns: int) -> None:
    try:
        with vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = db.get(GrvtTestnetBotRun,run_id); saved = _read(db,row)
            if saved['terminal']:
                if saved['terminal']['status'] == status: return
                raise ValueError('immutable terminal')
            payload = {'run_id':run_id, 'header_digest':row.payload_digest, 'selection_digest':row.selection_digest,
                'at_ns':now_ns, 'status':status, 'cause':cause}
            row.terminal_json, row.terminal_digest = _encode(payload); row.active_slot = None
            db.flush(); _read(db,row); db.commit()
    except Exception:
        raise GrvtTestnetBracketError('Final del ciclo no verificable; consulta sus órdenes') from None


def read_bot_run(vault, run_id: str) -> dict:
    try:
        with vault.session() as db: return _read(db, db.get(GrvtTestnetBotRun,run_id))
    except Exception:
        raise GrvtTestnetBracketError('Ciclo GRVT archivado no verificable') from None


def bot_run_history(vault, sub_account_id: str, *, offset: int = 0) -> dict:
    try:
        if type(offset) is not int or not 0 <= offset <= 1_000_000: raise ValueError('offset')
        with vault.session() as db:
            rows = list(db.scalars(select(GrvtTestnetBotRun).where(
                GrvtTestnetBotRun.sub_account_id == sub_account_id).order_by(
                GrvtTestnetBotRun.started_at_ns.desc(),GrvtTestnetBotRun.id.desc()).offset(offset).limit(21)))
            return {'items':[_read(db,row) for row in rows[:20]], 'offset':offset, 'has_more':len(rows)>20}
    except Exception:
        raise GrvtTestnetBracketError('Historial de ciclos GRVT no verificable') from None


def verify_all_bot_runs(db) -> None:
    for row in db.scalars(select(GrvtTestnetBotRun)): _read(db,row)
