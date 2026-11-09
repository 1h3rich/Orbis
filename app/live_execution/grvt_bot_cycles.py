"""Ciclos de una concesión GRVT TESTNET: selección → recibo → rearme.

El presupuesto reserva el tope completo de cada intento, incluso si su
respuesta se pierde. Ganancias y pérdidas provienen sólo de recibos financieros.
Ninguna lectura restaura autoridad o limpia una parada manual.
"""

from decimal import Decimal, localcontext
import json
from uuid import UUID

from sqlalchemy import select

from app.live_execution.grvt_testnet_oco import _encode, _stamp
from app.live_execution.grvt_testnet_preflight import client_id_for_signal
from app.live_execution.grvt_testnet_signal import SIGNAL_MAX_AGE_NS
from app.portfolio.models import (GrvtTestnetBotRun, GrvtTestnetBotCycle, GrvtTestnetBracketGroup,
    GrvtTestnetBracketReconciliation, GrvtTestnetBracketEmergency, GrvtTestnetReconciliation,
    GrvtTestnetOrderIntent, GrvtTestnetStopEvent)


LIMIT_STATUSES = {'ENTRY_LIMIT','BUDGET_LIMIT','LOSS_LIMIT'}


def _ns_floor(value: int) -> int:
    # STOP usa el Timestamp UTC existente, con resolución de microsegundos.
    return value//1000*1000


def limit_status(saved: dict) -> str | None:
    if saved['completed_entries'] != saved['entry_count']: return None
    with localcontext() as context:
        context.prec = 100
        if Decimal(saved['net_realized_pnl_usdt']) <= -Decimal(saved['loss_cap']): return 'LOSS_LIMIT'
        if saved['entry_count'] >= saved['max_entries']: return 'ENTRY_LIMIT'
        if Decimal(saved['entry_budget_reserved'])+Decimal(saved['entry_cap']) > Decimal(saved['total_entry_cap']): return 'BUDGET_LIMIT'
    return None


def _receipts(db, parent: str, at: int) -> tuple[list, str]:
    from app.live_execution.grvt_exit_watch_archive import _require_flat_receipt
    from app.live_execution.grvt_testnet_bracket_journal import _verified
    from app.live_execution.grvt_testnet_bracket_finance import verified_bracket_reconciliation
    from app.live_execution.grvt_testnet_bracket_emergency import read_emergency_attempts
    from app.live_execution.grvt_testnet_reconciliation import verified_reconciliation, _ns
    _require_flat_receipt(db,parent,at)
    group = db.get(GrvtTestnetBracketGroup,parent)
    first = verified_bracket_reconciliation(db,group,_verified(db,group))
    if first is None or first['reconciled_at_ns'] > at: raise ValueError('missing group receipt')
    row = db.get(GrvtTestnetBracketReconciliation,parent)
    references = [['BRACKET',parent,row.payload_digest]]
    with localcontext() as context:
        context.prec = 100
        net = Decimal(first['result']['net_realized_pnl_usdt'])
        for attempt in read_emergency_attempts(db,parent):
            if attempt['status'] != 'RECONCILED': raise ValueError('pending reduction')
            row = db.get(GrvtTestnetReconciliation,attempt['client_order_id'])
            receipt = verified_reconciliation(db,db.get(GrvtTestnetOrderIntent,row.client_order_id))
            if _ns(row.reconciled_at) > at: raise ValueError('receipt from future')
            result = receipt['result']
            # Los recibos IOC históricos omiten el campo neto cuando no hay
            # financiación; sus componentes siguen estando conciliados.
            net += (Decimal(result['reported_realized_pnl_usdt'])-Decimal(result['fees_usdt'])
                -Decimal(result.get('funding_paid_usdt','0')))
            references.append(['IOC',row.client_order_id,row.payload_digest])
        return references,format(net,'f')


def _stop_anchor(db, sub: str, at: int, anchor: dict | None = None) -> dict:
    from app.live_execution.grvt_stop_control import verified_testnet_stop
    state = verified_testnet_stop(db,sub)
    revision = anchor['revision'] if anchor is not None else state['revision']
    row = db.get(GrvtTestnetStopEvent,(sub,revision))
    if row is None: raise ValueError('missing cycle stop provenance')
    value = json.loads(row.payload_json)
    if (anchor is not None and (set(anchor) != {'revision','digest'} or anchor['digest'] != row.payload_digest)
            or not value['state']['stopped'] or value['at_ns'] > at): raise ValueError('invalid cycle stop anchor')
    return {'revision':revision,'digest':row.payload_digest}


def _outcome(db, header, cycle, selection, at, anchor=None):
    _stamp(at)
    if at < selection['selected_at_ns']: raise ValueError('outcome before entry')
    receipts, net = _receipts(db,cycle.parent_client_order_id,at)
    return {'run_id':header['id'],'sequence':cycle.sequence,'selection_digest':cycle.selection_digest,
        'completed_at_ns':at,'receipts':receipts,'net_realized_pnl_usdt':net,
        'stop_anchor':_stop_anchor(db,header['sub_account_id'],at,anchor)}


def _head(row, count, digest):
    if count:
        row.selection_json,row.selection_digest = _encode({'schema':'orbis.grvt.testnet.bot.cycles.head.v1',
            'run_id':row.id,'count':count,'tail_digest':digest})


def read_bounded_run(db, row, header):
    from app.live_execution.grvt_bot_archive import check_bot_selection_group, STATUSES, CAUSES
    from app.live_execution.grvt_stop_control import verified_testnet_stop
    cycles = []; previous = None; armed = header['started_at_ns']; total = Decimal(0); tail = None; last_at = armed
    for cycle in db.scalars(select(GrvtTestnetBotCycle).where(GrvtTestnetBotCycle.run_id == row.id).order_by(GrvtTestnetBotCycle.sequence)):
        selection = json.loads(cycle.selection_json); sequence = len(cycles)+1
        if (set(selection) != {'run_id','header_digest','sequence','previous_digest','action_request_id',
                'parent_client_order_id','recorded_at_ns','selected_at_ns'}
                or cycle.sequence != sequence or selection['sequence'] != sequence or type(selection['sequence']) is not int
                or selection['run_id'] != row.id or selection['header_digest'] != row.payload_digest
                or selection['previous_digest'] != previous or cycle.parent_client_order_id != selection['parent_client_order_id']
                or str(UUID(selection['action_request_id'])) != selection['action_request_id']
                or client_id_for_signal(header['source']['session_id'],selection['action_request_id']) != cycle.parent_client_order_id
                or _encode(selection) != (cycle.selection_json,cycle.selection_digest)):
            raise ValueError('invalid bot cycle selection')
        for key in ('recorded_at_ns','selected_at_ns'): _stamp(selection[key])
        if (not armed < selection['recorded_at_ns'] <= selection['selected_at_ns'] <= header['entry_deadline_ns']
                or selection['selected_at_ns']-selection['recorded_at_ns'] > SIGNAL_MAX_AGE_NS
                or cycles and cycles[-1]['rearm'] is None): raise ValueError('old or overlapping bot cycle')
        check_bot_selection_group(db,header,selection)
        outcome = None; rearm = None; tail = cycle.selection_digest; last_at = selection['selected_at_ns']
        if cycle.outcome_json is not None:
            outcome = json.loads(cycle.outcome_json)
            expected = _outcome(db,header,cycle,selection,outcome['completed_at_ns'],outcome['stop_anchor'])
            if _encode(expected) != (cycle.outcome_json,cycle.outcome_digest): raise ValueError('changed cycle receipt')
            with localcontext() as context:
                context.prec = 100; total += Decimal(outcome['net_realized_pnl_usdt'])
            tail = cycle.outcome_digest; last_at = outcome['completed_at_ns']
        elif cycle.outcome_digest is not None: raise ValueError('cycle outcome truncated')
        if cycle.rearm_json is not None:
            rearm = json.loads(cycle.rearm_json)
            if (outcome is None or set(rearm) != {'run_id','sequence','outcome_digest','rearmed_at_ns','stop_revision','stop_digest'}
                    or rearm['run_id'] != row.id or rearm['sequence'] != sequence
                    or rearm['outcome_digest'] != cycle.outcome_digest
                    or type(rearm['stop_revision']) is not int or rearm['stop_revision'] != outcome['stop_anchor']['revision']+1
                    or _encode(rearm) != (cycle.rearm_json,cycle.rearm_digest)):
                raise ValueError('invalid cycle rearm')
            _stamp(rearm['rearmed_at_ns'])
            verified_testnet_stop(db,header['sub_account_id'])
            event = db.get(GrvtTestnetStopEvent,(header['sub_account_id'],rearm['stop_revision']))
            stop = json.loads(event.payload_json) if event else None
            if (event is None or event.payload_digest != rearm['stop_digest'] or stop['request']['reason'] != 'AUTO_REARM'
                    or stop['request']['parent_client_order_id'] != cycle.parent_client_order_id or stop['state']['stopped']
                    or outcome['completed_at_ns'] > rearm['rearmed_at_ns']
                    or not _ns_floor(outcome['completed_at_ns']) <= stop['at_ns'] <= rearm['rearmed_at_ns'] <= header['entry_deadline_ns']):
                raise ValueError('cycle rearm without exact release')
            armed = last_at = rearm['rearmed_at_ns']; tail = previous = cycle.rearm_digest
        elif cycle.rearm_digest is not None: raise ValueError('cycle rearm truncated')
        cycles.append({'sequence':sequence,'selection':selection,'outcome':outcome,'rearm':rearm})
    count = len(cycles)
    if count:
        if _encode({'schema':'orbis.grvt.testnet.bot.cycles.head.v1','run_id':row.id,'count':count,'tail_digest':tail}) != (row.selection_json,row.selection_digest):
            raise ValueError('bot cycles truncated')
    elif row.selection_json is not None or row.selection_digest is not None: raise ValueError('missing bot cycles')
    with localcontext() as context:
        context.prec = 100; reserved = Decimal(header['entry_cap'])*count
    if count > header['max_entries'] or reserved > Decimal(header['total_entry_cap']): raise ValueError('entry budget exceeded')
    progress = {'cycles':cycles,'selection':cycles[-1]['selection'] if cycles else None,'entry_count':count,
        'completed_entries':sum(item['outcome'] is not None for item in cycles),
        'entry_budget_reserved':format(reserved,'f'),'net_realized_pnl_usdt':format(total,'f')}
    # Una cadena no puede continuar después de haber consumido sus límites.
    for n, cycle in enumerate(cycles):
        if cycle['rearm'] is not None:
            with localcontext() as context:
                context.prec = 100
                partial = {**header,'entry_count':n+1,'completed_entries':n+1,
                    'entry_budget_reserved':format(Decimal(header['entry_cap'])*(n+1),'f'),
                    'net_realized_pnl_usdt':format(sum((Decimal(item['outcome']['net_realized_pnl_usdt']) for item in cycles[:n+1]),Decimal(0)),'f')}
            if limit_status(partial): raise ValueError('rearm after a limit')
    terminal = None
    if row.terminal_json is not None:
        terminal = json.loads(row.terminal_json)
        if (set(terminal) != {'run_id','header_digest','selection_digest','at_ns','status','cause'}
                or terminal['run_id'] != row.id or terminal['header_digest'] != row.payload_digest
                or terminal['selection_digest'] != row.selection_digest or terminal['status'] not in STATUSES-{'COMPLETE'}
                or terminal['cause'] not in CAUSES or terminal['at_ns'] < last_at
                or _encode(terminal) != (row.terminal_json,row.terminal_digest)):
            raise ValueError('bounded bot terminal changed')
        _stamp(terminal['at_ns'])
        if terminal['status'] in LIMIT_STATUSES and limit_status({**header,**progress}) != terminal['status']:
            raise ValueError('unverified bot limit')
    elif row.terminal_digest is not None: raise ValueError('bounded terminal truncated')
    if row.active_slot != (None if terminal else header['sub_account_id']): raise ValueError('bounded reservation changed')
    return {**header,**progress,'terminal':terminal,'status':terminal['status'] if terminal else 'INTERRUPTED','authorization':False}


def select_bounded_open(db, row, saved, candidate, now):
    if (saved['terminal'] is not None or saved['entry_count'] != saved['completed_entries']
            or limit_status(saved) or saved['cycles'] and saved['cycles'][-1]['rearm'] is None):
        raise ValueError('no further entry authorized')
    sequence = saved['entry_count']+1
    prior = db.get(GrvtTestnetBotCycle,(row.id,sequence-1)) if sequence > 1 else None
    payload = {'run_id':row.id,'header_digest':row.payload_digest,'sequence':sequence,
        'previous_digest':prior.rearm_digest if prior else None,**candidate,'selected_at_ns':now}
    encoded,digest = _encode(payload)
    db.add(GrvtTestnetBotCycle(run_id=row.id,sequence=sequence,parent_client_order_id=candidate['parent_client_order_id'],
        selection_json=encoded,selection_digest=digest))
    _head(row,sequence,digest); db.flush(); read_bounded_run(db,row,{key:saved[key] for key in json.loads(row.payload_json)})


def complete_bot_cycle(vault, run_id: str, *, now_ns: int) -> dict:
    from app.live_execution.grvt_bot_archive import _read, _header
    with vault.session() as db:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = db.get(GrvtTestnetBotRun,run_id); saved = _read(db,row)
        if saved['terminal'] is not None or not saved['entry_count']: raise ValueError('cycle absent or stopped')
        cycle = db.get(GrvtTestnetBotCycle,(run_id,saved['entry_count']))
        if cycle.outcome_json is not None: return saved
        outcome = _outcome(db,_header(row),cycle,json.loads(cycle.selection_json),now_ns)
        cycle.outcome_json,cycle.outcome_digest = _encode(outcome)
        _head(row,cycle.sequence,cycle.outcome_digest); db.flush(); result = _read(db,row); db.commit(); return result


def rearm_bot_cycle(vault, run_id: str, *, clock_ns, require_permission) -> dict:
    from app.live_execution.grvt_bot_archive import _read
    from app.live_execution.grvt_stop_control import release_automatic_stop
    with vault.session() as db:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        require_permission(); row = db.get(GrvtTestnetBotRun,run_id); saved = _read(db,row)
        now = clock_ns(); _stamp(now)
        if (saved['terminal'] is not None or not saved['entry_count'] or limit_status(saved)
                or saved['entry_count'] != saved['completed_entries'] or now > saved['entry_deadline_ns']):
            raise ValueError('rearm outside authorization')
        cycle = db.get(GrvtTestnetBotCycle,(run_id,saved['entry_count']))
        if cycle.rearm_json is not None: raise ValueError('rearm already consumed')
        outcome = json.loads(cycle.outcome_json)
        release = release_automatic_stop(db,saved['sub_account_id'],cycle.parent_client_order_id,
            expected_revision=outcome['stop_anchor']['revision'],now_ns=now)
        event = db.get(GrvtTestnetStopEvent,(saved['sub_account_id'],release['revision']))
        payload = {'run_id':run_id,'sequence':cycle.sequence,'outcome_digest':cycle.outcome_digest,
            'rearmed_at_ns':now,'stop_revision':release['revision'],'stop_digest':event.payload_digest}
        cycle.rearm_json,cycle.rearm_digest = _encode(payload)
        _head(row,cycle.sequence,cycle.rearm_digest); db.flush(); require_permission()
        result = _read(db,row); db.commit(); return result
