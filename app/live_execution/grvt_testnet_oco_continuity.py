"""Anclaje financiero OCO para el siguiente IOC; no concede permiso de POST."""

from dataclasses import asdict
from decimal import Decimal
import json

from sqlalchemy import select

from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError, _verified, _stamp
from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.portfolio.models import GrvtTestnetOcoGroup, GrvtTestnetOcoReconciliation, GrvtTestnetOcoObservation


def oco_receipts(db, sub_account_id: str):
    # Una copia v30–v35 se verifica con su esquema original antes de migrar.
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() < 36:
        return []
    result = []
    for row in db.scalars(select(GrvtTestnetOcoReconciliation)):
        group = db.get(GrvtTestnetOcoGroup,row.group_id)
        if group is None:
            raise GrvtTestnetOcoError('Recibo OCO huérfano')
        if group.sub_account_id == sub_account_id:
            try:
                value = int(row.reconciled_at_ns)
                if str(value) != row.reconciled_at_ns: raise ValueError('noncanonical time')
                _stamp(value)
            except Exception:
                raise GrvtTestnetOcoError('Reloj de recibo OCO no verificable') from None
            result.append((row,group))
    return sorted(result,key=lambda pair:(int(pair[0].reconciled_at_ns),pair[0].group_id))


def oco_anchor_data(db, row, group) -> dict:
    from app.live_execution.grvt_testnet_oco_finance import (
        verified_oco_reconciliation, verified_oco_audits, _source, _external, canonical_funding_rows,
    )
    plan = _verified(db,group)
    receipt = verified_oco_reconciliation(db,group,plan)
    audit = verified_oco_audits(db,group,plan)[-1]
    _,before,baseline = _source(db,group,plan)
    observation = db.scalar(select(GrvtTestnetOcoObservation).where(
        GrvtTestnetOcoObservation.group_id == group.id,
        GrvtTestnetOcoObservation.sequence == audit['observation_sequence']))
    fills = _external(baseline['fills']['rows'],baseline['order'])
    for leg in audit['legs']:
        fills.extend(_external(leg['fills']['rows'],leg['order']))
    return {'client_order_id':group.parent_client_order_id,'receipt_digest':row.payload_digest,
        'history_start_ns':before['observed_at_ns'],'account_event_time_ns':receipt['account_event_time_ns'],
        'order':baseline['order'],'fills':sorted(fills,key=lambda fill:fill['trade_id']),
        'funding':canonical_funding_rows(audit['legs'][0]['funding']['rows']),'oco_group_id':group.id,
        'oco_orders':json.loads(observation.payload_json)['orders']}


def read_oco_anchor_fills(vault, client, sub_account_id, anchor, *, now_ns):
    from app.live_execution.grvt_testnet_oco_finance import _external, _fill, canonical_observed_order
    observed = observe_oco_orders(vault,client,anchor.oco_group_id,clock_ns=client._clock_ns)
    if [canonical_observed_order(order) for order in observed['orders']] != [canonical_observed_order(order) for order in anchor.oco_orders]:
        raise GrvtTestnetOcoError('Las salidas OCO cambiaron desde su conciliación')
    parent = client.lookup_order(sub_account_id,anchor.client_order_id)
    actual = {key:getattr(parent,key) for key in anchor.order}
    if canonical_observed_order(actual) != canonical_observed_order(anchor.order):
        raise GrvtTestnetOcoError('La entrada OCO cambió desde su conciliación')
    audit = audit_testnet_fills(client,parent,start_time_ns=anchor.history_start_ns,now_ns=now_ns,end_time_ns=now_ns)
    own = [{key:format(value,'f') if isinstance(value,Decimal) else value for key,value in asdict(fill).items()}
           for fill in audit.fills]
    other = [{key:format(value,'f') if isinstance(value,Decimal) else value for key,value in asdict(fill).items()}
             for fill in audit.other_fills]
    rows = sorted(_external(own,anchor.order)+[_fill(fill,external=True) for fill in other],key=lambda fill:fill['trade_id'])
    if not audit.sizes_match or rows != anchor.fills:
        raise GrvtTestnetOcoError('Historial posterior al OCO tiene cambios o actividad no explicada')
    return rows,audit.pages_drained
