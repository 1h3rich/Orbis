"""Recibo OTOCO como ancla cronológica; nunca permiso para saltarse historiales."""

from dataclasses import asdict
from decimal import Decimal
import json

from sqlalchemy import select

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import _verified
from app.live_execution.grvt_testnet_bracket_finance import verified_bracket_reconciliation,verified_bracket_audits
from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_oco import _stamp
from app.live_execution.grvt_testnet_oco_finance import _external,_fill,canonical_observed_order,canonical_funding_rows
from app.portfolio.models import GrvtTestnetBracketReconciliation,GrvtTestnetBracketGroup,GrvtTestnetBracketClaim,GrvtTestnetBracketObservation


def bracket_receipts(db,sub_account_id):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<40: return []
    result=[]
    for row in db.scalars(select(GrvtTestnetBracketReconciliation)):
        group=db.get(GrvtTestnetBracketGroup,row.parent_client_order_id)
        if group is None: raise GrvtTestnetBracketError('Recibo OTOCO huérfano')
        if group.sub_account_id==sub_account_id:
            try:
                stamp=int(row.reconciled_at_ns)
                if str(stamp)!=row.reconciled_at_ns: raise ValueError('noncanonical receipt time')
                _stamp(stamp)
            except Exception:
                raise GrvtTestnetBracketError('Reloj de recibo OTOCO no verificable') from None
            result.append((row,group))
    return sorted(result,key=lambda pair:(int(pair[0].reconciled_at_ns),pair[0].parent_client_order_id))


def bracket_anchor_data(db,row,group):
    plan=_verified(db,group)
    receipt=verified_bracket_reconciliation(db,group,plan)
    audit=verified_bracket_audits(db,group,plan)[-1]
    before=json.loads(db.get(GrvtTestnetBracketClaim,group.parent_client_order_id).preflight_json)['state']
    observation=db.scalar(select(GrvtTestnetBracketObservation).where(
        GrvtTestnetBracketObservation.parent_client_order_id==group.parent_client_order_id,
        GrvtTestnetBracketObservation.sequence==audit['observation_sequence']))
    fills=[fill for leg in audit['legs'] for fill in _external(leg['fills']['rows'],leg['order'])]
    return {'client_order_id':group.parent_client_order_id,'receipt_digest':row.payload_digest,
        'history_start_ns':before['observed_at_ns'],'account_event_time_ns':receipt['account_event_time_ns'],
        'order':audit['legs'][0]['order'],'fills':sorted(fills,key=lambda fill:fill['trade_id']),
        'funding':canonical_funding_rows(audit['legs'][0]['funding']['rows']),
        'bracket_parent_id':group.parent_client_order_id,'bracket_orders':json.loads(observation.payload_json)['state']['orders']}


def read_bracket_anchor_fills(vault,client,sub_account_id,anchor,*,now_ns):
    observed=observe_bracket_orders(vault,client,anchor.bracket_parent_id,clock_ns=client._clock_ns)
    if [canonical_observed_order(row) for row in observed['orders']]!=[canonical_observed_order(row) for row in anchor.bracket_orders]:
        raise GrvtTestnetBracketError('Los miembros OTOCO cambiaron desde su conciliación')
    parent=client.lookup_order(sub_account_id,anchor.client_order_id)
    if canonical_observed_order({key:getattr(parent,key) for key in anchor.order})!=canonical_observed_order(anchor.order):
        raise GrvtTestnetBracketError('La entrada OTOCO cambió desde su conciliación')
    audit=audit_testnet_fills(client,parent,start_time_ns=anchor.history_start_ns,now_ns=now_ns,end_time_ns=now_ns)
    def safe(row): return {key:format(value,'f') if isinstance(value,Decimal) else value for key,value in asdict(row).items()}
    rows=sorted(_external([safe(fill) for fill in audit.fills],anchor.order)+[_fill(safe(fill),external=True) for fill in audit.other_fills],key=lambda row:row['trade_id'])
    if not audit.sizes_match or rows!=anchor.fills:
        raise GrvtTestnetBracketError('Historial posterior al OTOCO no explicado')
    return rows,audit.pages_drained
