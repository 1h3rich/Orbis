"""Verified DEX receipts → wallet ledger. No RPC, signatures, capital or FX."""
from dataclasses import dataclass
from datetime import datetime,timedelta
from decimal import Context,ROUND_HALF_EVEN,localcontext
import hmac,re
from uuid import UUID,uuid5
from sqlalchemy import select
from app.portfolio import models as m
from app.portfolio.domain import CALC_PRECISION,PortfolioError
from app.portfolio.dex_deployments import _identity
from app.portfolio.dex_verified_read import scope
from app.portfolio.wallet_funding import _decoded,_encoded,_time
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_funding_credit import _wallet_owner
from app.portfolio.repository import _digest,_from_row,append_event_in_transaction
from app.portfolio import wallet_swap_booking as shared

CONTRACT='DEX_RECEIPT_BOOKING_V1'
NAMESPACE=UUID('21398014-1bd0-42b7-b67d-7115bc01df4b')
V1_KINDS=('DEPLOYMENT','CONTROL','GAS_FUNDING','OPERATOR','GAS_RETURN')
KINDS=(*V1_KINDS,'DEPLOYMENT_RESOLUTION','NATIVE_RESOLUTION','CALL_RESOLUTION')

def _contract(kind):
    if kind=='CALL_RESOLUTION':return 'DEX_RECEIPT_BOOKING_V4'
    return CONTRACT if kind in V1_KINDS else 'DEX_RECEIPT_BOOKING_V3' if kind=='NATIVE_RESOLUTION' else 'DEX_RECEIPT_BOOKING_V2'


class DexBookingError(PortfolioError):pass


@dataclass(frozen=True)
class Source:
    kind:str
    id:str
    account_id:str
    wallet_digest:str
    plan_digest:str
    receipt:dict
    trade_terms:dict
    updated_at:datetime
    action:str
    principal_wei:str
    signer_digest:str
    nonce:int
    transaction_index:int|None=None
    economic_outcome:str|None=None
    original_kind:str|None=None
    original_id:str|None=None

    @property
    def receipt_digest(self):return _encoded(self.receipt)[1]


def _registry(db=None):
    from app.portfolio.dex_deployments import deployment_record
    from app.portfolio.dex_controls import control_record
    from app.portfolio.dex_gas_funding import funding_record
    from app.portfolio.dex_gas_return import return_record
    from app.portfolio.dex_operator_journal import intent_record
    from app.portfolio.dex_deployment_resolution import receipt_source
    values=dict(zip(V1_KINDS,((m.RealDexDeploymentIntent,deployment_record),(m.RealDexControlIntent,control_record),
        (m.RealDexGasFundingIntent,funding_record),(m.RealDexOperatorIntent,intent_record),(m.RealDexGasReturnIntent,return_record))))
    values['DEPLOYMENT_RESOLUTION']=(m.RealDexDeploymentResolution,receipt_source)
    from app.portfolio.dex_native_resolution import receipt_source as native_source
    values['NATIVE_RESOLUTION']=(m.RealDexNativeResolution,native_source)
    if db is not None and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
        from app.portfolio.dex_call_booking import source_record as call_source
        values['CALL_RESOLUTION']=(m.RealDexCallResolution,call_source)
    return values


def _schema(db):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<66:raise DexBookingError('Actualiza la bóveda para contabilizar recibos DEX')


def claimed_event_ids(db,*,excluding=None):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<66:return set()
    query=select(m.RealDexReceiptBookingEvent.event_id)
    if excluding is not None:query=query.where(m.RealDexReceiptBookingEvent.booking_id!=excluding)
    result=set(db.scalars(query))
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78:
        result.update(db.scalars(select(m.RealDexExternalBookingMember.event_id)))
    return result


def source_record(db,kind,identifier):
    _schema(db)
    if kind not in KINDS:raise DexBookingError('Fuente de recibo DEX no compatible')
    if kind=='CALL_RESOLUTION':
        if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<71:raise DexBookingError('Actualiza la bóveda para contabilizar llamadas resueltas')
        from app.portfolio.dex_call_booking import source_record as call_source
        return call_source(db,identifier)
    identifier=str(UUID(identifier));_,load=_registry()[kind]
    with scope(db):
        row,terms,state=load(db,identifier)
        if row.status not in {'FINALIZED','REVERTED'} or state['receipt'] is None:raise DexBookingError('Comprueba primero el recibo finalizado')
        owner=terms['owner_digest'] if kind=='OPERATOR' else terms['wallet_digest']
        action=terms.get('action',kind);trade={'id':row.id,'action':'APPROVE'}
        if kind=='OPERATOR':trade=operator_trade_terms(db,row,terms)
        principal=state['receipt'].get('transferred_wei','0') if kind in {'GAS_FUNDING','GAS_RETURN','NATIVE_RESOLUTION'} else '0'
        signer=terms['signer_digest'] if kind=='NATIVE_RESOLUTION' else row.wallet_digest
        return Source(kind,row.id,row.account_id,owner,row.payload_digest,state['receipt'],trade,row.updated_at,action,principal,signer,int(terms['nonce']))


def operator_trade_terms(db,row,terms):
    action=terms['action'];trade={'id':row.id,'action':'APPROVE'}
    if action not in {'BUY','SELL'}:return trade
    from app.portfolio.dex_bot_journal import run_record
    decision=db.scalar(select(m.RealDexBotDecision).where(m.RealDexBotDecision.payload_digest==terms['decision_ref']))
    binding=db.scalar(select(m.RealDexBotBinding).where(m.RealDexBotBinding.grant_id==row.grant_id))
    if binding is not None and decision is None:raise DexBookingError('Falta la decisión causal propia del bot')
    if decision is not None:
        run,_,_,_=run_record(db,decision.run_id);event=_decoded(decision.payload_json,decision.payload_digest)
        if (run.account_id!=row.account_id or run.grant_id!=row.grant_id or event['execution'] is None
                or event['plan']['action']!=action or any(terms.get(key)!=value for key,value in event['execution'].items())):
            raise DexBookingError('El recibo no coincide con su decisión causal')
    return {**trade,'action':'SWAP','token_in':'USDT' if action=='BUY' else 'WBNB','amount_units':terms['trade']['amount']}


def booking_id(kind,identifier):
    if kind not in KINDS:raise DexBookingError('Fuente DEX no compatible')
    return str(uuid5(NAMESPACE,kind+':'+str(UUID(identifier))))


def _owner_ref(db,source,account):return _identity(db,'booking-owner',source.wallet_digest+':'+account)


def _context(db,kind,identifier,account,wallet):
    source=source_record(db,kind,identifier)
    if source.account_id!=str(UUID(account)) or not hmac.compare_digest(source.wallet_digest,_identity(db,'wallet',canonical_wallet_address(wallet))):
        raise DexBookingError('El recibo pertenece a otro contexto o wallet')
    return source


def booking_record(db,identifier):
    _schema(db);row=db.get(m.RealDexReceiptBooking,str(UUID(identifier)))
    if row is None:return None
    try:
        source=source_record(db,row.source_kind,row.source_id);payload=_decoded(row.payload_json,row.payload_digest)
        keys={'contract','source_kind','source_id','context_account_id','account_id','wallet_digest','owner_ref','plan_digest',
              'receipt_digest','revision_before','booked_at','events'}
        if (set(payload)!=keys or row.id!=booking_id(source.kind,source.id) or payload['contract']!=_contract(source.kind)
                or payload['source_kind']!=source.kind or payload['source_id']!=source.id or payload['context_account_id']!=source.account_id
                or payload['account_id']!=row.account_id or payload['wallet_digest']!=source.wallet_digest
                or payload['owner_ref']!=_owner_ref(db,source,row.account_id) or payload['plan_digest']!=source.plan_digest
                or payload['receipt_digest']!=source.receipt_digest or not re.fullmatch(r'[0-9a-f]{64}',payload['revision_before'])
                or _time(datetime.fromisoformat(payload['booked_at']))<source.updated_at or db.get(m.RealAccount,row.account_id) is None):raise ValueError
        refs=payload['events'];children=list(db.scalars(select(m.RealDexReceiptBookingEvent).where(m.RealDexReceiptBookingEvent.booking_id==row.id)))
        if not isinstance(refs,list) or not 1<=len(refs)<=100 or len(children)!=len(refs) or len({ref['id'] for ref in refs})!=len(refs):raise ValueError
        linked={child.event_id:child.role for child in children}
        used=claimed_event_ids(db,excluding=row.id)|set(db.scalars(select(m.RealWalletSwapBookingEvent.event_id)))|set(db.scalars(select(m.RealMexcFillBooking.event_id)))
        at=_time(datetime.fromisoformat(source.receipt['block_at']));trades=[];gas=0
        with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
            trade=shared._trade(source.trade_terms,source.receipt,at)
            for ref in refs:
                event=db.get(m.RealEvent,ref['id'])
                if (set(ref)!={'id','digest','role','existing'} or type(ref['existing']) is not bool or event is None
                        or event.account_id!=row.account_id or event.id in used or linked.get(event.id)!=ref['role']
                        or ref['digest']!=event.payload_digest or ref['digest']!=_digest(_from_row(event).input)):raise ValueError
                item=_from_row(event).input
                if (not ref['existing'] and item.occurred_at!=at or ref['existing'] and abs(item.occurred_at-at)>timedelta(minutes=2)):raise ValueError
                if ref['role']=='TRADE':
                    if not shared._matches_trade(item,trade) or not ref['existing'] and item!=trade:raise ValueError
                    trades.append(item)
                elif ref['role']=='GAS':gas+=shared._gas_units(item)
                else:raise ValueError
        if gas!=int(source.receipt['gas_fee_wei']) or len(trades)!=(1 if trade else 0):raise ValueError
    except (KeyError,TypeError,ValueError,PortfolioError,AttributeError):
        raise DexBookingError('El asiento DEX cambió o perdió su recibo verificado') from None
    return row,payload


def status(db,kind,identifier):
    stored=booking_record(db,booking_id(kind,identifier))
    if stored is None:return {'booked':False}
    row,payload=stored
    return {'booked':True,'booking_id':row.id,'target_account_id':row.account_id,'target_account_name':db.get(m.RealAccount,row.account_id).name,
        'events_created':0,'events_count':len(payload['events']),'booked_at':payload['booked_at'],'event_ids':[ref['id'] for ref in payload['events']]}


def _require_prior_trades(db,source,*,wallet=None):
    if wallet is not None and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
        from app.portfolio.dex_call_booking import require_trade_order
        if require_trade_order(db,source,wallet):return
    if source.kind!='OPERATOR' or source.action not in {'BUY','SELL'} or source.receipt['status']!='FINALIZED':return
    height=int(source.receipt['block_number'])
    with scope(db):
        for row in db.scalars(select(m.RealDexOperatorIntent).where(m.RealDexOperatorIntent.status=='FINALIZED')):
            if row.id==source.id:continue
            other=source_record(db,'OPERATOR',row.id)
            if other.wallet_digest!=source.wallet_digest or other.action not in {'BUY','SELL'}:continue
            before=int(other.receipt['block_number'])<height
            if int(other.receipt['block_number'])==height:
                if other.signer_digest!=source.signer_digest:
                    raise DexBookingError('Varias operadoras en el mismo bloque requieren conciliar el orden de ejecución')
                before=other.nonce<source.nonce
            if before and not status(db,'OPERATOR',other.id)['booked']:
                raise DexBookingError('Contabiliza primero la ejecución anterior '+other.id[:8])


def _proposal(db,source,wallet,trade_event_id,gas_event_ids):
    _require_prior_trades(db,source,wallet=wallet)
    owner=_wallet_owner(db,canonical_wallet_address(wallet))
    if owner is None or db.scalar(select(m.RealMexcAccountLink.account_ref).where(m.RealMexcAccountLink.account_id==owner.id)) is not None:
        raise DexBookingError('Vincula la wallet a una cuenta REAL propia, separada del exchange')
    from app.portfolio.accounting_view import require_receipt_extension
    financial=source.trade_terms['action']=='SWAP' and source.receipt['status']=='FINALIZED'
    require_receipt_extension(db,owner.id,source.kind,source.id,gas_only=not financial)
    with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
        extra=({'economic_outcome':source.economic_outcome,'original_kind':source.original_kind,
            'original_id':source.original_id} if source.kind=='CALL_RESOLUTION' else {})
        return shared._proposal(db,source,source.trade_terms,{'receipt':source.receipt},owner,
            trade_event_id=trade_event_id,gas_event_ids=gas_event_ids,contract=_contract(source.kind),
            extras={'source_kind':source.kind,'source_digest':source.plan_digest,'receipt_digest':source.receipt_digest,
                'internal_principal_bnb':str(shared._amount(source.principal_wei)),'principal_creates_event':False,**extra})


def preview_booking(db,kind,identifier,*,context_account_id,wallet_address,trade_event_id=None,gas_event_ids=None):
    with scope(db):
        source=_context(db,kind,identifier,context_account_id,wallet_address)
        existing=status(db,kind,identifier)
        if existing['booked']:return existing
        return _proposal(db,source,wallet_address,trade_event_id,gas_event_ids)[0]


def book_receipt(db,kind,identifier,*,context_account_id,wallet_address,expected_revision,preview_digest,now,trade_event_id=None,gas_event_ids=None):
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now)
        source=_context(db,kind,identifier,context_account_id,wallet_address);existing=status(db,kind,identifier)
        if existing['booked']:return existing
        proposal,items,linked=_proposal(db,source,wallet_address,trade_event_id,gas_event_ids)
        if proposal['revision']!=expected_revision or proposal['preview_digest']!=preview_digest:raise DexBookingError('La cartera cambió; vuelve a revisar el recibo')
        if not proposal['available']:raise DexBookingError('Enlaza primero los asientos ya registrados')
        if now<source.updated_at:raise DexBookingError('El reloj de registro retrocedió')
        identifier=booking_id(kind,source.id);target=proposal['target_account_id']
        refs=[{'id':event.id,'digest':_digest(event.input),'role':role,'existing':True} for role,event in linked]
        for index,(role,item) in enumerate(items):
            event=append_event_in_transaction(db,target,str(uuid5(UUID(identifier),f'{_contract(kind)}:{index}')),item)
            refs.append({'id':event.id,'digest':_digest(item),'role':role,'existing':False})
        payload={'contract':_contract(kind),'source_kind':kind,'source_id':source.id,'context_account_id':source.account_id,'account_id':target,
            'wallet_digest':source.wallet_digest,'owner_ref':_owner_ref(db,source,target),'plan_digest':source.plan_digest,
            'receipt_digest':source.receipt_digest,'revision_before':expected_revision,'booked_at':now.isoformat(),'events':refs}
        raw,digest=_encoded(payload);db.add(m.RealDexReceiptBooking(id=identifier,source_kind=kind,source_id=source.id,account_id=target,payload_json=raw,payload_digest=digest));db.flush()
        for ref in refs:db.add(m.RealDexReceiptBookingEvent(event_id=ref['id'],booking_id=identifier,role=ref['role']))
        db.flush()
        from app.portfolio.repository import account_snapshot
        account_snapshot(db,target)
        result={**status(db,kind,source.id),'events_created':len(items)};db.commit();return result
    except Exception:db.rollback();raise


def list_receipts(db,*,account_id,wallet_address):
    _schema(db);account_id=str(UUID(account_id));wallet=_identity(db,'wallet',canonical_wallet_address(wallet_address));result=[]
    if db.get(m.RealAccount,account_id) is None:raise DexBookingError('Cuenta REAL no encontrada')
    with scope(db):
        for kind,(model,_) in _registry(db).items():
            if kind=='DEPLOYMENT_RESOLUTION' and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<67:continue
            if kind=='NATIVE_RESOLUTION' and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<68:continue
            query=select(model).where(model.account_id==account_id,model.status.in_(['FINALIZED','REVERTED'])).order_by(model.created_at,model.id)
            for row in db.scalars(query):
                source=source_record(db,kind,row.id)
                if source.wallet_digest!=wallet:continue
                result.append({'source_kind':kind,'source_id':source.id,'action':source.action,'status':source.receipt['status'],
                    **({'economic_outcome':source.economic_outcome,'original_kind':source.original_kind,'original_id':source.original_id} if kind=='CALL_RESOLUTION' else {}),
                    'occurred_at':source.receipt['block_at'],'block_number':int(source.receipt['block_number']),'nonce':source.nonce,'gas_amount':str(shared._amount(source.receipt['gas_fee_wei'])),
                    'internal_principal_bnb':str(shared._amount(source.principal_wei)),**status(db,kind,source.id)})
    return sorted(result,key=lambda value:(value['block_number'],value['nonce'],value['source_kind'],value['source_id']))


def verify_all_bookings(db,*,version):
    names=('real_dex_receipt_bookings','real_dex_receipt_booking_events')
    if version<66:
        tables={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(name in tables and db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one() for name in names):
            raise DexBookingError('Una bóveda anterior no admite asientos de recibos DEX')
        return
    with scope(db):
        for row in db.scalars(select(m.RealDexReceiptBooking)):booking_record(db,row.id)
