"""Auditoría conjunta de dos salidas OCO; ningún acuse se convierte en fill."""

from __future__ import annotations

import json
import time
from decimal import Decimal, localcontext
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from app.live_execution.grvt_testnet_account import audit_testnet_account
from app.live_execution.grvt_testnet_continuity import _HISTORIES
from app.live_execution.grvt_testnet_evidence import _payload, _verified_payload
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_funding import linked_testnet_funding
from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError, _verified, _parent, _encode, _stamp
from app.live_execution.grvt_testnet_oco_observation import latest_observation, observe_oco_orders
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient, GrvtTestnetOrderObservation
from app.live_execution.grvt_testnet_preflight import verified_archived_preflight
from app.live_execution.grvt_testnet_reconciliation import _number, _positions, _isolated
from app.live_execution.grvt_testnet_recovery import _fill_summary
from app.portfolio.models import (
    GrvtTestnetOcoGroup, GrvtTestnetOcoClaim, GrvtTestnetOcoObservation, GrvtTestnetOcoAudit,
    GrvtTestnetPreflightEvidence, GrvtTestnetAuditEvidence, GrvtTestnetOcoReconciliation,
)
from app.portfolio.vault import RealVault


_AMOUNTS = {"size","price","fee","realized_pnl","builder_fee"}
_FILL_FIELDS = {"trade_id","event_time_ns",*_AMOUNTS,"fee_currency","is_taker"}
_EXTERNAL_FIELDS = (_FILL_FIELDS-{"is_taker"})|{"order_id","client_order_id","instrument","is_buyer","is_liquidation"}
_TERMINAL = {"FILLED","CANCELLED","REJECTED"}


def _canonical_number(value: str) -> str:
    text = format(_number(value),'f')
    text = text.rstrip('0').rstrip('.') if '.' in text else text
    return '0' if Decimal(text) == 0 else text


def _fill(row: dict, *, external: bool = False) -> dict:
    if not isinstance(row,dict) or set(row) != (_EXTERNAL_FIELDS if external else _FILL_FIELDS):
        raise ValueError('fill fields')
    if external and (type(row['is_buyer']) is not bool or row['is_liquidation'] is not False):
        raise ValueError('invalid external fill flags')
    return {key:_canonical_number(value) if key in _AMOUNTS else value for key,value in row.items()}


def canonical_observed_order(order: dict) -> dict:
    return {key:_canonical_number(value) if key in ('size','traded_size') else value for key,value in order.items()}


def canonical_funding_rows(rows: list[dict]) -> list[dict]:
    return [{**row,'amount_paid':_canonical_number(row['amount_paid'])} for row in rows]


def _external(rows: list[dict], order: dict) -> list[dict]:
    return [{**{key:value for key,value in _fill(row).items() if key != 'is_taker'},
        'order_id':order['order_id'],'client_order_id':order['client_order_id'],
        'instrument':order['instrument'],'is_buyer':order['side'] == 'BUY','is_liquidation':False} for row in rows]


def _source(db, group, plan):
    receipt,_ = _parent(db,group.parent_client_order_id)
    before = verified_archived_preflight(db,db.get(GrvtTestnetPreflightEvidence,group.parent_client_order_id))
    baseline = _verified_payload(db.get(GrvtTestnetAuditEvidence,receipt['audits'][-1][0]))
    claim = db.get(GrvtTestnetOcoClaim,group.id)
    if claim is None or claim.preflight_json is None:
        raise GrvtTestnetOcoError('No hay preflight de envío OCO verificable')
    return receipt,before,baseline


def _calculate_audit(db, group, plan, p: dict) -> dict:
    """Se repite al leer/backupear; no se confía sólo en un SHA recalculado."""
    receipt,before,baseline = _source(db,group,plan)
    observation = db.scalar(select(GrvtTestnetOcoObservation).where(
        GrvtTestnetOcoObservation.group_id == group.id,
        GrvtTestnetOcoObservation.sequence == p['observation_sequence']))
    latest_observation(db,group,plan)
    if (observation is None or observation.payload_digest != p['observation_digest']
            or type(p['observation_sequence']) is not int
            or type(p['cutoff_ns']) is not int or type(p['observed_at_ns']) is not int
            or not 0 <= p['observed_at_ns']-p['cutoff_ns'] <= 5_000_000_000
            or p['cutoff_ns'] < json.loads(observation.payload_json)['observed_at_ns']
            or not isinstance(p['legs'],list) or len(p['legs']) != 2):
        raise ValueError('audit scope')
    _stamp(p['cutoff_ns']); _stamp(p['observed_at_ns'])
    observed = json.loads(observation.payload_json)
    start = before['observed_at_ns']
    own, summaries = [],[]
    sections = ('funding','transfers','withdrawals','deposits','interest','account','account_scope')
    for i,(leg,expected,state) in enumerate(zip(p['legs'],plan['legs'],observed['orders'])):
        order = leg['order']
        if (order != {'sub_account_id':group.sub_account_id,'client_order_id':expected['client_order_id'],
                'order_id':state['order_id'],'status':state['status'],'instrument':plan['instrument'],
                'side':plan['side'],'size':plan['size'],'traded_size':state['traded_size'],
                'update_time_ns':state['update_time_ns']}
                or order['status'] not in _TERMINAL or leg['complete_for_execution'] is not False
                or any(leg[key] != p['legs'][0][key] for key in sections)):
            raise ValueError('inconsistent joint snapshot')
        summary = _fill_summary(leg)
        if summary is None or summary['sizes_match_order'] is not True or Decimal(summary['builder_fee']) != 0:
            raise ValueError('incomplete fills')
        if any(type(leg[name].get('observed_until_ns')) is not int
               or leg[name]['observed_until_ns'] != p['cutoff_ns']
               for name in ('funding','transfers','withdrawals','deposits','interest')):
            raise ValueError('histories do not share the fixed cutoff')
        canonical = [_fill(row) for row in leg['fills']['rows']]
        for row in canonical:
            if (type(row['event_time_ns']) is not int or not int(state['create_time_ns']) <= row['event_time_ns'] <= min(int(state['update_time_ns']),p['cutoff_ns'])
                    or _number(row['price']) <= 0
                    or (plan['side'] == 'BUY' and _number(row['price']) > Decimal(expected['limit_price']))
                    or (plan['side'] == 'SELL' and _number(row['price']) < Decimal(expected['limit_price']))):
                raise ValueError('fill outside signed order')
        own.append(canonical); summaries.append(summary)
        # Tras contrastar la partición completa más abajo, sólo se reutilizan
        # las guardas de cobertura y movimientos del circuito individual.
        partitioned = {**leg,'fills':{**leg['fills'],'external_rows':[],
            'external_fill_count':0,'external_activity_detected':False}}
        _isolated(partitioned,start,strict=True)
    parent_rows = _external(baseline['fills']['rows'],baseline['order'])
    all_ids = [row['trade_id'] for rows in (parent_rows,*own) for row in rows]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError('shared trade identity')
    for i,leg in enumerate(p['legs']):
        external = [_fill(row,external=True) for row in leg['fills']['external_rows']]
        expected = parent_rows+_external(own[1-i],p['legs'][1-i]['order'])
        if (sorted(external,key=lambda x:x['trade_id']) != sorted(expected,key=lambda x:x['trade_id'])
                or type(leg['fills']['external_fill_count']) is not int
                or leg['fills']['external_fill_count'] != len(external)
                or leg['fills']['external_activity_detected'] is not bool(external)):
            raise ValueError('unexplained or missing fills')
    funding = linked_testnet_funding(p['legs'][0]['funding'],own[0]+own[1],instrument=plan['instrument'],start_ns=start,end_ns=p['cutoff_ns'])
    account,scope = p['legs'][0]['account'],p['legs'][0]['account_scope']
    event = account['event_time_ns']
    if (type(event) is not int or not p['cutoff_ns'] <= event <= p['observed_at_ns']+2_000_000_000
            or not -2_000_000_000 <= p['observed_at_ns']-event <= 5_000_000_000
            or account['settle_currency'] != 'USDT' or type(account['open_order_count']) is not int
            or account['open_order_count'] != 0 or scope['margin_type'] != 'SIMPLE_CROSS_MARGIN'
            or scope['sub_account_mode'] != 'SINGLE_ASSET_MODE'
            or not isinstance(scope['other_balances'],list) or any(_number(row[1]) != 0 for row in scope['other_balances'])):
        raise ValueError('account scope')
    with localcontext() as ctx:
        ctx.prec = 100
        filled = sum((_number(summary['filled_size']) for summary in summaries),Decimal(0))
        if filled > Decimal(plan['size']):
            raise ValueError('overfilled source')
        fees = Decimal(0)
        for summary in summaries:
            for currency,amount in summary['fees_by_currency']:
                if currency != 'USDT': raise ValueError('foreign fee')
                fees += _number(amount)
        pnl = sum((_number(summary['realized_pnl_observed']) for summary in summaries),Decimal(0))
        paid = Decimal(funding['paid_usdt'])
        position = _positions(receipt['result']['positions'])[plan['instrument']]+filled*(1 if plan['side'] == 'BUY' else -1)
        expected_positions = {plan['instrument']:position} if position else {}
        balance = _number(account['settle_balance'])
        if (_positions(account['positions']) != expected_positions
                or balance != _number(receipt['result']['settle_balance'])+pnl-fees-paid
                or _number(account['initial_margin']) < 0 or _number(account['maintenance_margin']) < 0
                or _number(account['total_equity']) != balance+_number(scope['unrealized_pnl'])
                or not position and any(_number(value) != 0 for value in
                    (account['initial_margin'],account['maintenance_margin'],scope['unrealized_pnl']))):
            raise ValueError('unexplained account delta')
        values = {'settle_balance':balance,'filled_size':filled,'fees_usdt':fees,'funding_paid_usdt':paid,
            'reported_realized_pnl_usdt':pnl,'net_realized_pnl_usdt':pnl-fees-paid}
        return {**{key:_canonical_number(format(value,'f')) for key,value in values.items()},
            'positions':[[name,_canonical_number(format(size,'f'))] for name,size in expected_positions.items()]}


def verified_oco_audits(db, group, plan) -> list[dict]:
    try:
        rows = list(db.scalars(select(GrvtTestnetOcoAudit).where(GrvtTestnetOcoAudit.group_id == group.id).order_by(GrvtTestnetOcoAudit.sequence)))
        result,previous = [],None
        for seq,row in enumerate(rows,1):
            p = json.loads(row.payload_json)
            if (_encode(p) != (row.payload_json,row.payload_digest) or row.sequence != seq
                    or set(p) != {'schema','group_id','plan_digest','sequence','previous_digest','observation_sequence',
                                  'observation_digest','cutoff_ns','observed_at_ns','legs'}
                    or p['schema'] != 'orbis.grvt.testnet.oco.audit.v1' or p['group_id'] != group.id
                    or p['plan_digest'] != group.payload_digest or type(p['sequence']) is not int or p['sequence'] != seq
                    or p['previous_digest'] != (previous.payload_digest if previous is not None else None)
                    or result and p['cutoff_ns'] < result[-1]['cutoff_ns']):
                raise ValueError('invalid audit chain')
            calculation = _calculate_audit(db,group,plan,p)
            if result:
                old = result[-1]
                for previous_leg,current_leg in zip(old['legs'],p['legs']):
                    current = {fill['trade_id']:_fill(fill) for fill in current_leg['fills']['rows']}
                    if any(current.get(fill['trade_id']) != _fill(fill) for fill in previous_leg['fills']['rows']):
                        raise ValueError('previous fill changed or disappeared')
                current_funding = {tuple(payment[key] for key in ('tx_id','event_time_ns','instrument','currency')):payment
                    for payment in canonical_funding_rows(p['legs'][0]['funding']['rows'])}
                if any(current_funding.get(tuple(payment[key] for key in ('tx_id','event_time_ns','instrument','currency'))) != payment
                       for payment in canonical_funding_rows(old['legs'][0]['funding']['rows'])):
                    raise ValueError('previous funding changed or disappeared')
            result.append({**p,'id':row.id,'result':calculation,'causal_proof':False,'real_book_events_created':False})
            previous = row
        return result
    except Exception:
        raise GrvtTestnetOcoError('Auditorías financieras OCO no verificables') from None


def read_oco_audits(vault: RealVault, group_id: str) -> list[dict]:
    with vault.session() as db:
        group = db.get(GrvtTestnetOcoGroup,group_id)
        if group is None: raise GrvtTestnetOcoError('Grupo OCO no encontrado')
        return verified_oco_audits(db,group,_verified(db,group))


def audit_oco(vault: RealVault, client: GrvtTestnetOrderClient, group_id: str, *, clock_ns: Callable[[],int] = time.time_ns) -> dict:
    """Lectura únicamente. Fija corte antes de agotar historiales y leer cuenta."""
    if read_oco_reconciliation(vault,group_id) is not None:
        raise GrvtTestnetOcoError('OCO ya conciliado; su recibo no se sobrescribe')
    observed = observe_oco_orders(vault,client,group_id,clock_ns=clock_ns)
    if any(row['status'] not in _TERMINAL for row in observed['orders']):
        raise GrvtTestnetOcoError('Ambas salidas deben ser terminales antes de conciliar')
    with vault.session() as db:
        group = db.get(GrvtTestnetOcoGroup,group_id)
        plan = _verified(db,group)
        _,before,baseline = _source(db,group,plan)
        verified_oco_audits(db,group,plan)
        plan_digest = group.payload_digest
    try:
        parent = client.lookup_order(group.sub_account_id,group.parent_client_order_id)
        actual = {key:getattr(parent,key) for key in baseline['order']}
        if canonical_observed_order(actual) != canonical_observed_order(baseline['order']):
            raise ValueError('parent order changed')
        cutoff = clock_ns(); _stamp(cutoff)
        orders = [GrvtTestnetOrderObservation(group.sub_account_id,row['client_order_id'],row['order_id'],row['status'],
            plan['instrument'],plan['side'],plan['size'],row['traded_size'],leg['limit_price'],row['update_time_ns'],
            create_time_ns=row['create_time_ns']) for row,leg in zip(observed['orders'],plan['legs'])]
        fills = [audit_testnet_fills(client,order,start_time_ns=before['observed_at_ns'],end_time_ns=cutoff,now_ns=cutoff) for order in orders]
        histories = {name:reader(client,group.sub_account_id,start_time_ns=before['observed_at_ns'],now_ns=cutoff)
                     for name,reader,_ in _HISTORIES}
        account = audit_testnet_account(client,orders[0],fills[0],now_ns=clock_ns(),
            funding=histories['funding'],transfers=histories['transfers'],withdrawals=histories['withdrawals'],interest=histories['interest'])
        legs = [_payload(order,fill,**histories,account=account) for order,fill in zip(orders,fills)]
        at = clock_ns(); _stamp(at)
    except Exception:
        raise GrvtTestnetOcoError('No se pudo verificar la auditoría conjunta; conserva el bloqueo OCO') from None
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group = db.get(GrvtTestnetOcoGroup,group_id)
            plan = _verified(db,group)
            if (group.payload_digest != plan_digest or latest_observation(db,group,plan) != observed
                    or db.get(GrvtTestnetOcoReconciliation,group_id) is not None):
                raise GrvtTestnetOcoError('Grupo OCO cambiado durante la auditoría')
            old = verified_oco_audits(db,group,plan)
            previous = db.scalar(select(GrvtTestnetOcoAudit).where(GrvtTestnetOcoAudit.group_id == group_id).order_by(GrvtTestnetOcoAudit.sequence.desc()).limit(1))
            p = {'schema':'orbis.grvt.testnet.oco.audit.v1','group_id':group_id,'plan_digest':plan_digest,
                'sequence':len(old)+1,'previous_digest':previous.payload_digest if previous else None,
                'observation_sequence':observed['sequence'],'observation_digest':_encode(observed)[1],
                'cutoff_ns':cutoff,'observed_at_ns':at,'legs':legs}
            encoded,digest = _encode(p)
            db.add(GrvtTestnetOcoAudit(id=str(uuid4()),group_id=group_id,sequence=p['sequence'],payload_json=encoded,payload_digest=digest))
            db.flush(); result = verified_oco_audits(db,group,plan)[-1]
            db.commit(); return result
        except GrvtTestnetOcoError:
            db.rollback(); raise
        except Exception:
            db.rollback()
            raise GrvtTestnetOcoError('Saldo, fills o posición OCO no conciliables; conserva el bloqueo') from None


def _receipt_payload(db, group, plan, *, at_ns: int) -> dict:
    _stamp(at_ns)
    audits = verified_oco_audits(db,group,plan)
    if len(audits) < 2:
        raise GrvtTestnetOcoError('Se requieren dos auditorías OCO independientes')
    first,last = audits[-2:]
    if (last['observed_at_ns']-first['observed_at_ns'] < 5_000_000_000
            or not 0 <= at_ns-last['observed_at_ns'] <= 30_000_000_000):
        raise GrvtTestnetOcoError('Audita de nuevo: separación mínima 5 s y última lectura de hasta 30 s')
    if (first['result'] != last['result']
            or any(canonical_observed_order(a['order']) != canonical_observed_order(b['order'])
                   or [_fill(row) for row in a['fills']['rows']] != [_fill(row) for row in b['fills']['rows']]
                   for a,b in zip(first['legs'],last['legs']))
            or canonical_funding_rows(first['legs'][0]['funding']['rows']) != canonical_funding_rows(last['legs'][0]['funding']['rows'])):
        raise GrvtTestnetOcoError('Auditorías OCO aún inestables; conserva el bloqueo')
    rows = list(db.scalars(select(GrvtTestnetOcoAudit).where(GrvtTestnetOcoAudit.group_id == group.id).order_by(GrvtTestnetOcoAudit.sequence)))
    return {'schema':'orbis.grvt.testnet.oco.reconciliation.v1',
        'scope':'ISOLATED_TESTNET_OCO_BALANCE_POSITION_MATCH','group_id':group.id,
        'sub_account_id':group.sub_account_id,'plan_digest':group.payload_digest,
        'reconciled_at_ns':at_ns,'audits':[[row.id,row.payload_digest] for row in rows[-2:]],
        'account_event_time_ns':last['legs'][0]['account']['event_time_ns'],'result':last['result'],
        'causal_proof':False,'mainnet_order_sent':False,'real_book_events_created':False}


def verified_oco_reconciliation(db, group, plan) -> dict | None:
    row = db.get(GrvtTestnetOcoReconciliation,group.id)
    if row is None: return None
    try:
        if not isinstance(row.reconciled_at_ns,str) or not row.reconciled_at_ns.isdecimal():
            raise ValueError('invalid receipt time')
        payload = _receipt_payload(db,group,plan,at_ns=int(row.reconciled_at_ns))
        if _encode(payload) != (row.payload_json,row.payload_digest):
            raise ValueError('receipt changed')
        return payload
    except Exception:
        raise GrvtTestnetOcoError('Recibo financiero OCO no verificable') from None


def read_oco_reconciliation(vault: RealVault, group_id: str) -> dict | None:
    with vault.session() as db:
        group = db.get(GrvtTestnetOcoGroup,group_id)
        if group is None: raise GrvtTestnetOcoError('Grupo OCO no encontrado')
        return verified_oco_reconciliation(db,group,_verified(db,group))


def reconcile_oco(vault: RealVault, group_id: str, *, now_ns: int) -> dict:
    """Recibo explícito e idempotente. No quita STOP ni crea patrimonio REAL."""
    _stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group = db.get(GrvtTestnetOcoGroup,group_id)
            if group is None: raise GrvtTestnetOcoError('Grupo OCO no encontrado')
            plan = _verified(db,group)
            existing = verified_oco_reconciliation(db,group,plan)
            if existing is not None:
                db.commit(); return existing
            _parent(db,group.parent_client_order_id,latest=True)
            payload = _receipt_payload(db,group,plan,at_ns=now_ns)
            encoded,digest = _encode(payload)
            db.add(GrvtTestnetOcoReconciliation(group_id=group_id,reconciled_at_ns=str(now_ns),payload_json=encoded,payload_digest=digest))
            db.flush(); result = verified_oco_reconciliation(db,group,plan)
            db.commit(); return result
        except Exception:
            db.rollback(); raise
