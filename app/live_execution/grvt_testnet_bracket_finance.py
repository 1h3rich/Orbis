"""Auditoría de tres órdenes desde cuenta plana. No libera capital por sí sola."""

from decimal import Decimal,localcontext
import json
import time
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from app.live_execution.grvt_testnet_account import audit_testnet_account
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import _verified
from app.live_execution.grvt_testnet_bracket_observation import latest_bracket_observation,observe_bracket_orders
from app.live_execution.grvt_testnet_continuity import _HISTORIES
from app.live_execution.grvt_testnet_evidence import _payload
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_funding import linked_testnet_funding
from app.live_execution.grvt_testnet_oco import _encode,_stamp
from app.live_execution.grvt_testnet_oco_finance import _fill,_external,_canonical_number,canonical_funding_rows,canonical_observed_order
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,GrvtTestnetOrderObservation
from app.live_execution.grvt_testnet_reconciliation import _number,_positions,_isolated
from app.live_execution.grvt_testnet_recovery import _fill_summary
from app.portfolio.models import GrvtTestnetBracketGroup,GrvtTestnetBracketClaim,GrvtTestnetBracketObservation,GrvtTestnetBracketAudit,GrvtTestnetBracketAuditHead,GrvtTestnetBracketReconciliation
from app.portfolio.vault import RealVault


_TERMINAL={'FILLED','CANCELLED','REJECTED'}


def _calculate(db,group,plan,p):
    claim=db.get(GrvtTestnetBracketClaim,group.parent_client_order_id)
    if claim is None: raise ValueError('preflight absent')
    before=json.loads(claim.preflight_json)['state']
    record=db.scalar(select(GrvtTestnetBracketObservation).where(
        GrvtTestnetBracketObservation.parent_client_order_id==group.parent_client_order_id,
        GrvtTestnetBracketObservation.sequence==p['observation_sequence']))
    latest_bracket_observation(db,group,plan)
    if (record is None or record.payload_digest!=p['observation_digest'] or type(p['observation_sequence']) is not int
            or type(p['cutoff_ns']) is not int or type(p['observed_at_ns']) is not int
            or not 0<=p['observed_at_ns']-p['cutoff_ns']<=5_000_000_000
            or not isinstance(p['legs'],list) or len(p['legs'])!=3): raise ValueError('audit scope')
    state=json.loads(record.payload_json)['state']; start=before['observed_at_ns']
    _stamp(p['cutoff_ns']); _stamp(p['observed_at_ns'])
    if p['cutoff_ns']<state['observed_at_ns'] or before['positions']!=[]: raise ValueError('invalid baseline')
    own,summaries=[],[]
    sections=('funding','transfers','withdrawals','deposits','interest','account','account_scope')
    for evidence,expected,actual in zip(p['legs'],plan['legs'],state['orders']):
        order=evidence['order']
        if (order!={'sub_account_id':group.sub_account_id,'client_order_id':expected['client_order_id'],
                'order_id':actual['order_id'],'status':actual['status'],'instrument':plan['instrument'],
                'side':expected['side'],'size':actual['size'],'traded_size':actual['traded_size'],
                'update_time_ns':actual['update_time_ns']}
                or order['status'] not in _TERMINAL or evidence['complete_for_execution'] is not False
                or any(evidence[key]!=p['legs'][0][key] for key in sections)):
            raise ValueError('different joint snapshot')
        summary=_fill_summary(evidence)
        if summary is None or summary['sizes_match_order'] is not True or Decimal(summary['builder_fee'])!=0:
            raise ValueError('incomplete fills')
        for name in ('funding','transfers','withdrawals','deposits','interest'):
            if type(evidence[name].get('observed_until_ns')) is not int or evidence[name]['observed_until_ns']!=p['cutoff_ns']:
                raise ValueError('different history cutoff')
        rows=[_fill(row) for row in evidence['fills']['rows']]
        for row in rows:
            price=_number(row['price'])
            if (type(row['event_time_ns']) is not int
                    or not int(actual['create_time_ns'])<=row['event_time_ns']<=min(int(actual['update_time_ns']),p['cutoff_ns'])
                    or price<=0 or (expected['side']=='BUY' and price>Decimal(expected['limit_price']))
                    or (expected['side']=='SELL' and price<Decimal(expected['limit_price']))):
                raise ValueError('fill outside signed bounds')
        own.append(rows); summaries.append(summary)
        _isolated({**evidence,'fills':{**evidence['fills'],'external_rows':[],
            'external_fill_count':0,'external_activity_detected':False}},start,strict=True)
    identifiers=[row['trade_id'] for rows in own for row in rows]
    if len(identifiers)!=len(set(identifiers)): raise ValueError('duplicate trade identity')
    for index,evidence in enumerate(p['legs']):
        external=[_fill(row,external=True) for row in evidence['fills']['external_rows']]
        expected=[row for other in range(3) if other!=index for row in _external(own[other],p['legs'][other]['order'])]
        if (sorted(external,key=lambda row:row['trade_id'])!=sorted(expected,key=lambda row:row['trade_id'])
                or type(evidence['fills']['external_fill_count']) is not int
                or evidence['fills']['external_fill_count']!=len(external)
                or evidence['fills']['external_activity_detected'] is not bool(external)):
            raise ValueError('unexplained or missing fills')
    all_fills=[row for rows in own for row in rows]
    funding=linked_testnet_funding(p['legs'][0]['funding'],all_fills,instrument=plan['instrument'],start_ns=start,end_ns=p['cutoff_ns'])
    account,scope=p['legs'][0]['account'],p['legs'][0]['account_scope']
    event=account['event_time_ns']
    if (type(event) is not int or not p['cutoff_ns']<=event<=p['observed_at_ns']+2_000_000_000
            or not -2_000_000_000<=p['observed_at_ns']-event<=5_000_000_000
            or account['settle_currency']!='USDT' or type(account['open_order_count']) is not int
            or account['open_order_count']!=0 or scope['margin_type']!='SIMPLE_CROSS_MARGIN'
            or scope['sub_account_mode']!='SINGLE_ASSET_MODE' or not isinstance(scope['other_balances'],list)
            or any(_number(row[1])!=0 for row in scope['other_balances'])): raise ValueError('account scope')
    with localcontext() as ctx:
        ctx.prec=100
        entry=_number(summaries[0]['filled_size'])
        exits=sum((_number(item['filled_size']) for item in summaries[1:]),Decimal(0))
        if exits>entry or _number(summaries[0]['realized_pnl_observed'])!=0: raise ValueError('invalid reduction or entry pnl')
        if own[0] and any(row['event_time_ns']<max(fill['event_time_ns'] for fill in own[0]) for rows in own[1:] for row in rows):
            raise ValueError('exit before complete parent fill')
        fees=Decimal(0)
        for summary in summaries:
            for currency,amount in summary['fees_by_currency']:
                if currency!='USDT': raise ValueError('foreign fee')
                fees+=_number(amount)
        pnl=sum((_number(summary['realized_pnl_observed']) for summary in summaries),Decimal(0))
        paid=Decimal(funding['paid_usdt']); residual=(entry-exits)*(1 if plan['legs'][0]['side']=='BUY' else -1)
        positions={plan['instrument']:residual} if residual else {}
        balance=_number(account['settle_balance'])
        if (_positions(account['positions'])!=positions or balance!=_number(before['settle_balance'])+pnl-fees-paid
                or _number(account['initial_margin'])<0 or _number(account['maintenance_margin'])<0
                or _number(account['total_equity'])!=balance+_number(scope['unrealized_pnl'])
                or not residual and any(_number(value)!=0 for value in
                    (account['initial_margin'],account['maintenance_margin'],scope['unrealized_pnl']))):
            raise ValueError('unexplained account difference')
        values={'settle_balance':balance,'entry_filled_size':entry,'exit_filled_size':exits,'fees_usdt':fees,
            'funding_paid_usdt':paid,'reported_realized_pnl_usdt':pnl,'net_realized_pnl_usdt':pnl-fees-paid}
        return {**{key:_canonical_number(format(value,'f')) for key,value in values.items()},
            'positions':[[name,_canonical_number(format(size,'f'))] for name,size in positions.items()]}


def verified_bracket_audits(db,group,plan) -> list[dict]:
    try:
        rows=list(db.scalars(select(GrvtTestnetBracketAudit).where(
            GrvtTestnetBracketAudit.parent_client_order_id==group.parent_client_order_id).order_by(GrvtTestnetBracketAudit.sequence)))
        head=db.get(GrvtTestnetBracketAuditHead,group.parent_client_order_id)
        if head is None:
            if rows: raise ValueError('missing audit head')
            return []
        if type(head.count) is not int or head.count!=len(rows) or not rows: raise ValueError('truncated audit chain')
        result=[]; digest=None
        for seq,row in enumerate(rows,1):
            p=json.loads(row.payload_json)
            if (_encode(p)!=(row.payload_json,row.payload_digest) or row.sequence!=seq
                    or set(p)!={'schema','parent_client_order_id','plan_digest','sequence','previous_digest',
                        'observation_sequence','observation_digest','cutoff_ns','observed_at_ns','legs'}
                    or p['schema']!='orbis.grvt.testnet.bracket.audit.v1'
                    or p['parent_client_order_id']!=group.parent_client_order_id or p['plan_digest']!=group.payload_digest
                    or type(p['sequence']) is not int or p['sequence']!=seq or p['previous_digest']!=digest
                    or result and p['cutoff_ns']<result[-1]['cutoff_ns']): raise ValueError('invalid audit chain')
            calculation=_calculate(db,group,plan,p)
            if result:
                for old,new in zip(result[-1]['legs'],p['legs']):
                    current={fill['trade_id']:_fill(fill) for fill in new['fills']['rows']}
                    if any(current.get(fill['trade_id'])!=_fill(fill) for fill in old['fills']['rows']):
                        raise ValueError('old fill changed')
                current={tuple(row[key] for key in ('tx_id','event_time_ns','instrument','currency')):row
                    for row in canonical_funding_rows(p['legs'][0]['funding']['rows'])}
                if any(current.get(tuple(row[key] for key in ('tx_id','event_time_ns','instrument','currency')))!=row
                    for row in canonical_funding_rows(result[-1]['legs'][0]['funding']['rows'])):
                    raise ValueError('old funding changed')
            result.append({**p,'id':row.id,'result':calculation,'causal_proof':False,'real_book_events_created':False})
            digest=row.payload_digest
        if digest!=head.digest: raise ValueError('audit head changed')
        return result
    except Exception:
        raise GrvtTestnetBracketError('Auditorías financieras OTOCO no verificables') from None


def read_bracket_audits(vault: RealVault,parent_client_order_id: str) -> list[dict]:
    with vault.session() as db:
        group=db.get(GrvtTestnetBracketGroup,parent_client_order_id)
        if group is None: raise GrvtTestnetBracketError('Entrada OTOCO no encontrada')
        return verified_bracket_audits(db,group,_verified(db,group))


def audit_bracket(vault: RealVault,client: GrvtTestnetOrderClient,parent_client_order_id: str,*,
                   clock_ns: Callable[[],int]=time.time_ns) -> dict:
    with vault.session() as db:
        if db.get(GrvtTestnetBracketReconciliation,parent_client_order_id) is not None:
            raise GrvtTestnetBracketError('Grupo OTOCO ya conciliado; su recibo no se sobrescribe')
    observed=observe_bracket_orders(vault,client,parent_client_order_id,clock_ns=clock_ns)
    if any(row['status'] not in _TERMINAL for row in observed['orders']):
        raise GrvtTestnetBracketError('Los tres miembros deben ser terminales antes de conciliar')
    with vault.session() as db:
        group=db.get(GrvtTestnetBracketGroup,parent_client_order_id); plan=_verified(db,group)
        claim=db.get(GrvtTestnetBracketClaim,parent_client_order_id)
        start=json.loads(claim.preflight_json)['state']['observed_at_ns']
        digest=group.payload_digest; sequence=claim.observation_count; observation_digest=claim.observation_head_digest
        verified_bracket_audits(db,group,plan)
    try:
        cutoff=clock_ns(); _stamp(cutoff)
        orders=[GrvtTestnetOrderObservation(group.sub_account_id,row['client_order_id'],row['order_id'],row['status'],
            plan['instrument'],leg['side'],row['size'],row['traded_size'],leg['limit_price'],row['update_time_ns'],
            create_time_ns=row['create_time_ns']) for row,leg in zip(observed['orders'],plan['legs'])]
        fills=[audit_testnet_fills(client,order,start_time_ns=start,end_time_ns=cutoff,now_ns=cutoff) for order in orders]
        histories={name:reader(client,group.sub_account_id,start_time_ns=start,now_ns=cutoff) for name,reader,_ in _HISTORIES}
        account=audit_testnet_account(client,orders[0],fills[0],now_ns=clock_ns(),funding=histories['funding'],
            transfers=histories['transfers'],withdrawals=histories['withdrawals'],interest=histories['interest'])
        legs=[_payload(order,fill,**histories,account=account) for order,fill in zip(orders,fills)]
        at=clock_ns(); _stamp(at)
    except Exception:
        raise GrvtTestnetBracketError('No se pudo auditar el grupo; conserva el bloqueo OTOCO') from None
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group=db.get(GrvtTestnetBracketGroup,parent_client_order_id); plan=_verified(db,group)
            claim=db.get(GrvtTestnetBracketClaim,parent_client_order_id)
            if (group.payload_digest!=digest or latest_bracket_observation(db,group,plan)!=observed
                    or claim.observation_count!=sequence or claim.observation_head_digest!=observation_digest
                    or db.get(GrvtTestnetBracketReconciliation,parent_client_order_id) is not None):
                raise ValueError('group changed during audit')
            old=verified_bracket_audits(db,group,plan); head=db.get(GrvtTestnetBracketAuditHead,parent_client_order_id)
            payload={'schema':'orbis.grvt.testnet.bracket.audit.v1','parent_client_order_id':parent_client_order_id,
                'plan_digest':digest,'sequence':len(old)+1,'previous_digest':head.digest if head else None,
                'observation_sequence':sequence,'observation_digest':observation_digest,'cutoff_ns':cutoff,'observed_at_ns':at,'legs':legs}
            encoded,hashed=_encode(payload)
            db.add(GrvtTestnetBracketAudit(id=str(uuid4()),parent_client_order_id=parent_client_order_id,
                sequence=payload['sequence'],payload_json=encoded,payload_digest=hashed))
            if head is None: db.add(GrvtTestnetBracketAuditHead(parent_client_order_id=parent_client_order_id,count=1,digest=hashed))
            else: head.count+=1; head.digest=hashed
            db.flush(); result=verified_bracket_audits(db,group,plan)[-1]; db.commit(); return result
        except Exception:
            db.rollback()
            raise GrvtTestnetBracketError('Saldo, fills o posición OTOCO no conciliables; conserva el bloqueo') from None


def _receipt_payload(db,group,plan,*,now_ns,historical=False):
    _stamp(now_ns)
    audits=verified_bracket_audits(db,group,plan)
    if len(audits)<2: raise GrvtTestnetBracketError('Se requieren dos auditorías OTOCO independientes')
    first,last=audits[-2:]
    if (last['observed_at_ns']-first['observed_at_ns']<5_000_000_000
            or not 0<=now_ns-last['observed_at_ns']<=30_000_000_000):
        raise GrvtTestnetBracketError('Audita de nuevo: separación mínima 5 s y última lectura de hasta 30 s')
    if (first['result']!=last['result'] or any(canonical_observed_order(a['order'])!=canonical_observed_order(b['order'])
            or [_fill(row) for row in a['fills']['rows']]!=[_fill(row) for row in b['fills']['rows']]
            for a,b in zip(first['legs'],last['legs']))
            or canonical_funding_rows(first['legs'][0]['funding']['rows'])!=canonical_funding_rows(last['legs'][0]['funding']['rows'])):
        raise GrvtTestnetBracketError('Auditorías OTOCO inestables; conserva el bloqueo')
    actual=latest_bracket_observation(db,group,plan)
    if historical:
        # La cadena completa se verifica arriba. Una observación posterior
        # puede impedir otra operación, pero no cambia lo conocido al emitir.
        states=[json.loads(row.payload_json)['state'] for row in db.scalars(
            select(GrvtTestnetBracketObservation).where(
                GrvtTestnetBracketObservation.parent_client_order_id==group.parent_client_order_id
            ).order_by(GrvtTestnetBracketObservation.sequence))]
        actual=next((state for state in reversed(states) if state['observed_at_ns']<=now_ns),None)
    original=db.scalar(select(GrvtTestnetBracketObservation).where(
        GrvtTestnetBracketObservation.parent_client_order_id==group.parent_client_order_id,
        GrvtTestnetBracketObservation.sequence==last['observation_sequence']))
    if (actual is None or actual['observed_at_ns']>now_ns
            or actual['orders']!=json.loads(original.payload_json)['state']['orders']):
        raise GrvtTestnetBracketError('Los estados cambiaron después de la auditoría OTOCO')
    rows=list(db.scalars(select(GrvtTestnetBracketAudit).where(
        GrvtTestnetBracketAudit.parent_client_order_id==group.parent_client_order_id).order_by(GrvtTestnetBracketAudit.sequence)))
    return {'schema':'orbis.grvt.testnet.bracket.reconciliation.v1',
        'scope':'ISOLATED_TESTNET_BRACKET_BALANCE_POSITION_MATCH','parent_client_order_id':group.parent_client_order_id,
        'sub_account_id':group.sub_account_id,'plan_digest':group.payload_digest,'reconciled_at_ns':now_ns,
        'audits':[[row.id,row.payload_digest] for row in rows[-2:]],
        'account_event_time_ns':last['legs'][0]['account']['event_time_ns'],'result':last['result'],
        'causal_proof':False,'mainnet_order_sent':False,'real_book_events_created':False}


def verified_bracket_reconciliation(db,group,plan) -> dict | None:
    row=db.get(GrvtTestnetBracketReconciliation,group.parent_client_order_id)
    if row is None: return None
    try:
        if not isinstance(row.reconciled_at_ns,str) or not row.reconciled_at_ns.isascii() or not row.reconciled_at_ns.isdecimal():
            raise ValueError('invalid receipt timestamp')
        result=_receipt_payload(db,group,plan,now_ns=int(row.reconciled_at_ns),historical=True)
        if _encode(result)!=(row.payload_json,row.payload_digest): raise ValueError('changed receipt')
        return result
    except Exception:
        raise GrvtTestnetBracketError('Recibo financiero OTOCO no verificable') from None


def reconcile_bracket(vault: RealVault,parent_client_order_id: str,*,now_ns: int) -> dict:
    """Recibo explícito e inmutable; no borra STOP ni crea asientos REAL."""
    _stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group=db.get(GrvtTestnetBracketGroup,parent_client_order_id)
            if group is None: raise GrvtTestnetBracketError('Entrada OTOCO no encontrada')
            plan=_verified(db,group)
            receipt=verified_bracket_reconciliation(db,group,plan)
            if receipt is not None:
                db.commit(); return receipt
            payload=_receipt_payload(db,group,plan,now_ns=now_ns)
            encoded,digest=_encode(payload)
            db.add(GrvtTestnetBracketReconciliation(parent_client_order_id=parent_client_order_id,
                reconciled_at_ns=str(now_ns),payload_json=encoded,payload_digest=digest))
            db.flush(); result=verified_bracket_reconciliation(db,group,plan); db.commit(); return result
        except Exception:
            db.rollback(); raise
