"""Conciliación nativa MAINNET: contratos terminales, fills y cuenta plana.

Lee seis historiales con el mismo corte. No firma, cancela ni transfiere.
El recibo conserva dos auditorías independientes; nunca restaura autorización.
"""

from collections.abc import Callable
from decimal import Decimal,localcontext
import json
import re
import time

from sqlalchemy import select

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError,unsigned_mainnet_bracket,exact_uint
from app.live_execution.grvt_mainnet_journal import encode,_draft,_instrument,_control,read_mainnet_bracket
from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
from app.live_execution.grvt_mainnet_preflight import number,canonical,_state
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient,HISTORY_URLS
from app.portfolio.models import (GrvtMainnetBracket,GrvtMainnetAudit,GrvtMainnetAuditHead,
    GrvtMainnetReconciliation,GrvtMainnetSettlementControl)


_TABLES=('grvt_mainnet_audits','grvt_mainnet_audit_heads','grvt_mainnet_reconciliations','grvt_mainnet_settlement_controls')
_FILL_FIELDS={'trade_id','order_id','client_order_id','event_time_ns','is_buyer','is_taker',
    'size','price','fee','fee_currency','realized_pnl','builder_fee','is_liquidation','instrument'}
_FUND_FIELDS={'tx_id','event_time_ns','instrument','currency','amount_paid'}
_ACCOUNT_FIELDS={'sub_account_id','event_time_ns','observed_at_ns','margin_type','sub_account_mode',
    'settle_currency','positions','open_order_count','other_collateral','settle_balance','total_equity',
    'initial_margin','maintenance_margin','unrealized_pnl'}
_TERMINAL={'FILLED','CANCELLED','REJECTED'}
_ID=re.compile(r'[A-Za-z0-9_:\-]{1,128}\Z')


def stamp(value: object) -> int:
    if type(value) is not int or not 0<value<2**63: raise ValueError('financial timestamp')
    return value


def finance_schema(db,*,required: bool=False) -> bool:
    version=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    if version<51:
        if required: raise GrvtMainnetError('Actualiza el esquema de conciliación MAINNET')
        return False
    tables={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(_TABLES)<=tables: raise GrvtMainnetError('Esquema financiero MAINNET truncado')
    return True


def _fill(raw: dict,sub: str) -> dict:
    if not isinstance(raw,dict) or raw.get('sub_account_id')!=sub: raise ValueError('fill scope')
    value={key:raw[key] for key in _FILL_FIELDS-{'event_time_ns'}}
    event=raw['event_time']
    if not isinstance(event,str) or not re.fullmatch(r'[0-9]{1,20}',event): raise ValueError('fill time')
    value['event_time_ns']=stamp(int(event))
    for key in ('size','price','fee','realized_pnl','builder_fee'): value[key]=canonical(number(value[key]))
    return value


def verified_settlement_chain(db,sub: str) -> list[dict]:
    """Verifica enlaces y ancla; el replay financiero se hace por cada grupo."""
    if not finance_schema(db):return []
    records=list(db.scalars(select(GrvtMainnetReconciliation).join(GrvtMainnetBracket,
        GrvtMainnetBracket.parent_client_order_id==GrvtMainnetReconciliation.parent_client_order_id)
        .where(GrvtMainnetBracket.sub_account_id==sub)))
    values=[(record,json.loads(record.payload_json)) for record in records]
    if any(type(value.get('sub_sequence')) is not int for _,value in values):raise ValueError('settlement sequence')
    values.sort(key=lambda pair:pair[1]['sub_sequence']);previous=None;digest=None
    for index,(record,value) in enumerate(values,1):
        if (value['sub_sequence']!=index or value.get('sub_account_id')!=sub
                or value.get('parent_client_order_id')!=record.parent_client_order_id
                or value.get('previous_parent_client_order_id')!=previous
                or value.get('previous_receipt_digest')!=digest
                or encode(value)!=(record.payload_json,record.payload_digest)):
            raise ValueError('settlement chain truncated or changed')
        previous=record.parent_client_order_id;digest=record.payload_digest
    control=db.get(GrvtMainnetSettlementControl,sub)
    if (control is None and values or control is not None and (type(control.count) is not int
            or control.count!=len(values) or not values or control.parent_client_order_id!=previous or control.digest!=digest)):
        raise ValueError('settlement tail truncated')
    return [value for _,value in values]


def _funding(raw: dict,sub: str) -> dict:
    if not isinstance(raw,dict) or raw.get('sub_account_id')!=sub: raise ValueError('funding scope')
    event=raw['event_time']
    if not isinstance(event,str) or not re.fullmatch(r'[0-9]{1,20}',event): raise ValueError('funding time')
    return {'tx_id':raw['tx_id'],'event_time_ns':stamp(int(event)),'instrument':raw['instrument'],
        'currency':raw['currency'],'amount_paid':canonical(number(raw['amount']))}


def _histories(client: GrvtMainnetClient,sub: str,start: int,cutoff: int,checked: Callable[[],int]) -> dict:
    result={}
    for name in HISTORY_URLS:
        cursor=''; cursors=set(); rows=[]
        for page in range(1,21):
            checked(); value=client.history_page(name,sub,start_ns=start,end_ns=cutoff,cursor=cursor); checked()
            batch,following=value.get('result'),value.get('next')
            if (not isinstance(batch,list) or len(batch)>500 or not isinstance(following,str)
                    or len(following)>1024 or not following.isascii() or any(ord(c)<32 for c in following)):
                raise ValueError('incomplete history')
            if name=='fill_history': rows.extend(_fill(raw,sub) for raw in batch)
            elif name=='funding_payment_history': rows.extend(_funding(raw,sub) for raw in batch)
            elif batch: raise ValueError('external financial activity')
            if not following:
                rows.sort(key=lambda row:(row['event_time_ns'],row.get('trade_id',row.get('tx_id'))))
                result[name]={'pages':page,'drained':True,'rows':rows}; break
            if following==cursor or following in cursors: raise ValueError('repeated history cursor')
            cursors.add(following); cursor=following
        else: raise ValueError('history page limit')
    return result


def calculate_native_flat(claim: dict,observation: dict,histories: dict,account: dict,*,
        cutoff_ns: int,observed_at_ns: int) -> dict:
    """PNL exacto de un ciclo plano: diferencia de nocionales, sin dividir VWAP."""
    return _calculate_account(claim,observation,histories,account,cutoff_ns=cutoff_ns,observed_at_ns=observed_at_ns)


def calculate_reduction_account(claim: dict,observation: dict,histories: dict,account: dict,*,
        cutoff_ns: int,observed_at_ns: int,extra_orders=(),extra_templates=()) -> dict:
    """Comprueba cantidades y saldo para reducir; no certifica beneficio parcial."""
    return _calculate_account(claim,observation,histories,account,cutoff_ns=cutoff_ns,observed_at_ns=observed_at_ns,
        require_flat=False,allow_recovery=True,extra_orders=extra_orders,extra_templates=extra_templates)


def calculate_recovered_flat(claim,observation,histories,account,*,cutoff_ns,observed_at_ns,extra_orders,extra_templates):
    return _calculate_account(claim,observation,histories,account,cutoff_ns=cutoff_ns,observed_at_ns=observed_at_ns,
        require_flat=True,allow_recovery=True,extra_orders=extra_orders,extra_templates=extra_templates)


def _calculate_account(claim,observation,histories,account,*,cutoff_ns,observed_at_ns,
        require_flat=True,allow_recovery=False,extra_orders=(),extra_templates=()):
    draft=_draft(claim['draft']); instrument=_instrument(claim['instrument']); state=observation['state']
    start=claim['preflight']['states'][-1]['observed_at_ns']
    stamp(cutoff_ns);stamp(observed_at_ns)
    if (not start<=state['observed_at_ns']<=cutoff_ns<=observed_at_ns<=cutoff_ns+5_000_000_000
            or state['needs_review'] and (not allow_recovery or set(state['review_reasons'])-
                {'PARTIAL_EXIT','RESIDUAL_AFTER_TERMINAL_EXITS','EXIT_EXPIRED_LOCALLY'})
            or any(row['status'] not in _TERMINAL for row in [*state['orders'],*extra_orders])
            or len(extra_orders)!=len(extra_templates)
            or set(histories)!=set(HISTORY_URLS)):
        raise ValueError('non-terminal or non-flat group')
    templates=unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=claim['parent_client_order_id'],
        nonces=tuple(claim['nonces']),issued_at_ns=claim['issued_at_ns'])
    templates=[*templates,*extra_templates];orders=[*state['orders'],*extra_orders]
    if (len({order['client_order_id'] for order in orders})!=len(orders)
            or len({order['order_id'] for order in orders})!=len(orders)):
        raise ValueError('duplicate order identity')
    for name,section in histories.items():
        if (set(section)!={'pages','drained','rows'} or type(section['pages']) is not int
                or not 1<=section['pages']<=20 or section['drained'] is not True
                or not isinstance(section['rows'],list) or len(section['rows'])>section['pages']*500
                or name not in ('fill_history','funding_payment_history') and section['rows']):
            raise ValueError('financial history coverage')
    rows=histories['fill_history']['rows']; by_id={row['client_order_id']:(index,row,templates[index])
        for index,row in enumerate(orders)}
    seen=set(); sizes=[Decimal(0)]*len(orders); notionals=[Decimal(0)]*len(orders); fees=Decimal(0); reported=Decimal(0)
    entry_times=[]; exit_times=[]
    with localcontext() as context:
        context.prec=100
        for fill in rows:
            if (not isinstance(fill,dict) or set(fill)!=_FILL_FIELDS or not isinstance(fill['trade_id'],str)
                    or not _ID.fullmatch(fill['trade_id']) or fill['trade_id'] in seen
                    or fill['client_order_id'] not in by_id): raise ValueError('external or duplicate fill')
            index,order,template=by_id[fill['client_order_id']]; leg=template['legs'][0]
            size,price=number(fill['size']),number(fill['price']); at=stamp(fill['event_time_ns'])
            exact_uint(size,instrument.base_decimals,2**64-1)
            exact_uint(price,9,2**64-1)
            if (fill['order_id']!=order['order_id'] or fill['instrument']!=draft.instrument
                    or type(fill['is_buyer']) is not bool or fill['is_buyer']!=leg['is_buying_asset']
                    or type(fill['is_taker']) is not bool or fill['is_liquidation'] is not False
                    or fill['fee_currency']!='USDT' or number(fill['builder_fee'])!=0
                    or not start<=at<=cutoff_ns or not int(order['create_time_ns'])<=at<=int(order['update_time_ns'])
                    or size<=0 or price<=0
                    or (price>number(leg['limit_price']) if fill['is_buyer'] else price<number(leg['limit_price']))
                    or index==0 and number(fill['realized_pnl'])!=0): raise ValueError('fill outside original contract')
            seen.add(fill['trade_id']);sizes[index]+=size;notionals[index]+=size*price
            fees+=number(fill['fee']);reported+=number(fill['realized_pnl'])
            (entry_times if index==0 else exit_times).append(at)
        remaining=sizes[0]-sum(sizes[1:],Decimal(0))
        positions=[[draft.instrument,canonical(remaining*(1 if draft.direction=='LONG' else -1))]] if remaining else []
        if (any(sizes[index]!=number(order['traded_size']) for index,order in enumerate(orders))
                or remaining<0 or require_flat and remaining!=0
                or exit_times and (not entry_times or min(exit_times)<max(entry_times))):
            raise ValueError('fill quantity or causal mismatch')
        computed=(sum(notionals[1:],Decimal(0))-notionals[0])*(1 if draft.direction=='LONG' else -1) if remaining==0 else None
        if computed is not None and reported!=computed: raise ValueError('reported PNL differs from exact fill calculation')
        from app.live_execution.grvt_perp_funding_link import funding_trade_times
        paid=Decimal(0); funding_seen=set(); times=funding_trade_times(claim,rows)
        for payment in histories['funding_payment_history']['rows']:
            if (not isinstance(payment,dict) or set(payment)!=_FUND_FIELDS or payment['tx_id'] not in times
                    or payment['instrument']!=draft.instrument or payment['currency']!='USDT'
                    or not times[payment['tx_id']]<=stamp(payment['event_time_ns'])<=cutoff_ns):
                raise ValueError('unlinked funding')
            identity=(payment['tx_id'],payment['event_time_ns'],payment['instrument'],payment['currency'])
            if identity in funding_seen: raise ValueError('duplicate funding')
            funding_seen.add(identity);paid+=number(payment['amount_paid'])
        if (not isinstance(account,dict) or set(account)!=_ACCOUNT_FIELDS
                or account['sub_account_id']!=draft.sub_account_id or account['settle_currency']!='USDT'
                or account['margin_type']!='SIMPLE_CROSS_MARGIN' or account['sub_account_mode']!='SINGLE_ASSET_MODE'
                or account['positions']!=positions or account['other_collateral']!=[]
                or type(account['open_order_count']) is not int or account['open_order_count']!=0
                or not cutoff_ns<=stamp(account['event_time_ns'])<=observed_at_ns+2_000_000_000
                or not cutoff_ns<=stamp(account['observed_at_ns'])<=observed_at_ns
                or any(number(account[key])<0 for key in ('initial_margin','maintenance_margin'))
                or not remaining and any(number(account[key])!=0 for key in ('initial_margin','maintenance_margin','unrealized_pnl'))):
            raise ValueError('account not isolated and flat')
        balance=number(account['settle_balance']); baseline=number(claim['preflight']['states'][-1]['settle_balance'])
        net=reported-fees-paid
        if balance!=baseline+net or number(account['total_equity'])!=balance+number(account['unrealized_pnl']):
            raise ValueError('unexplained financial balance')
        if not require_flat:
            return {'scope':'REDUCTION_ONLY_ACCOUNT_MATCH','financially_reconciled':False,
                'remaining_size':canonical(remaining),'positions':positions,'settle_balance':canonical(balance),
                'reported_realized_pnl_usdt':canonical(reported),'reported_net_change_usdt':canonical(net),
                'computed_realized_pnl_usdt':canonical(computed) if computed is not None else None,
                'fees_usdt':canonical(fees),'funding_paid_usdt':canonical(paid)}
        return {key:canonical(value) for key,value in {'settle_balance':balance,'entry_filled_size':sizes[0],
            'exit_filled_size':sum(sizes[1:],Decimal(0)),'computed_realized_pnl_usdt':computed,
            'reported_realized_pnl_usdt':reported,'fees_usdt':fees,'funding_paid_usdt':paid,
            'net_realized_pnl_usdt':net}.items()}|{'positions':[]}


def verified_financial_evidence(db,row,claim: dict,observations: list[dict]) -> tuple[list[dict],dict|None]:
    if not finance_schema(db):return [],None
    result=[];previous=None
    for record in db.scalars(select(GrvtMainnetAudit).where(GrvtMainnetAudit.parent_client_order_id==row.parent_client_order_id)
            .order_by(GrvtMainnetAudit.sequence)):
        value=json.loads(record.payload_json)
        recovered=value.get('schema')=='orbis.grvt.mainnet.bracket.audit.v2'
        fields={'schema','network','parent_client_order_id','claim_digest','sequence','previous_digest',
                'observation_sequence','observation_digest','cutoff_ns','observed_at_ns','histories','account','result'}
        if recovered:fields|={'recovery_snapshot_sequence','recovery_snapshot_digest','reductions'}
        if (set(value)!=fields
                or value['schema'] not in ('orbis.grvt.mainnet.bracket.audit.v1','orbis.grvt.mainnet.bracket.audit.v2') or value['network']!='MAINNET'
                or value['parent_client_order_id']!=row.parent_client_order_id or value['claim_digest']!=row.payload_digest
                or type(value['sequence']) is not int or value['sequence']!=len(result)+1 or record.sequence!=value['sequence']
                or value['previous_digest']!=previous or encode(value)!=(record.payload_json,record.payload_digest)
                or type(value['observation_sequence']) is not int or not 1<=value['observation_sequence']<=len(observations)):
            raise ValueError('financial audit chain')
        observation=observations[value['observation_sequence']-1]
        if (encode(observation)[1]!=value['observation_digest']
                or result and value['cutoff_ns']<result[-1]['observed_at_ns']): raise ValueError('financial observation source')
        if recovered:
            from app.live_execution.grvt_mainnet_recovery import verified_recovery
            from app.live_execution.grvt_mainnet_reduction import snapshot_reductions
            view=verified_recovery(db,row,claim,observations)
            seq=value['recovery_snapshot_sequence']
            if view is None or type(seq) is not int or not 1<=seq<=len(view['events']):raise ValueError('recovery finance source')
            source=view['events'][seq-1]
            if (source['type']!='REDUCTION_SNAPSHOT' or encode(source)[1]!=value['recovery_snapshot_digest']
                    or any(source['details'][key]!=value[key] for key in
                        ('observation_sequence','observation_digest','histories','account','cutoff_ns','observed_at_ns'))):
                raise ValueError('recovery finance snapshot changed')
            templates,orders=snapshot_reductions(view,source)
            normalized=[{key:item for key,item in state.items() if key!='observed_at_ns'} for state in orders]
            if value['reductions']!=normalized or not orders:raise ValueError('recovered reduction states changed')
            expected=calculate_recovered_flat(claim,observation,value['histories'],value['account'],
                cutoff_ns=value['cutoff_ns'],observed_at_ns=value['observed_at_ns'],extra_orders=orders,extra_templates=templates)
        else:
            expected=calculate_native_flat(claim,observation,value['histories'],value['account'],
                cutoff_ns=value['cutoff_ns'],observed_at_ns=value['observed_at_ns'])
        if encode(expected)!=encode(value['result']):raise ValueError('financial calculation changed')
        previous=record.payload_digest;result.append(value)
    head=db.get(GrvtMainnetAuditHead,row.parent_client_order_id)
    if (head is None and result or head is not None and (head.count!=len(result) or head.digest!=previous or not result)):
        raise ValueError('financial audit head truncated')
    stored=db.get(GrvtMainnetReconciliation,row.parent_client_order_id);receipt=None
    if stored is not None:
        receipt=json.loads(stored.payload_json)
        expected=_receipt(row,claim,observations,result,now_ns=receipt['reconciled_at_ns'])
        chain=verified_settlement_chain(db,row.sub_account_id)
        link=next(value for value in chain if value['parent_client_order_id']==row.parent_client_order_id)
        expected.update({key:link[key] for key in ('sub_sequence','previous_parent_client_order_id','previous_receipt_digest')})
        if encode(expected)!=(stored.payload_json,stored.payload_digest):raise ValueError('financial receipt changed')
    return result,receipt


def _receipt(row,claim: dict,observations: list[dict],audits: list[dict],*,now_ns: int) -> dict:
    stamp(now_ns)
    if len(audits)<2:raise GrvtMainnetError('Se requieren dos auditorías financieras MAINNET')
    first,last=audits[-2:]
    if (last['observed_at_ns']-first['observed_at_ns']<5_000_000_000
            or not 0<=now_ns-last['observed_at_ns']<=30_000_000_000
            or first['result']!=last['result']
            or first['schema']!=last['schema'] or first.get('reductions',[])!=last.get('reductions',[])
            or any(first['histories'][key]['rows']!=last['histories'][key]['rows'] for key in HISTORY_URLS)
            or observations[first['observation_sequence']-1]['state']['orders']!=observations[last['observation_sequence']-1]['state']['orders']
            or observations[-1]['sequence']!=last['observation_sequence']):
        raise GrvtMainnetError('Auditorías MAINNET inestables o sin separación de 5 s; conserva el bloqueo')
    recovered=last['schema']=='orbis.grvt.mainnet.bracket.audit.v2'
    return {'schema':'orbis.grvt.mainnet.bracket.reconciliation.v2' if recovered else 'orbis.grvt.mainnet.bracket.reconciliation.v1','network':'MAINNET',
        'scope':'ISOLATED_RECOVERED_BRACKET_FLAT_BALANCE_MATCH' if recovered else 'ISOLATED_NATIVE_BRACKET_FLAT_BALANCE_MATCH','parent_client_order_id':row.parent_client_order_id,
        'claim_digest':row.payload_digest,'sub_account_id':row.sub_account_id,'reconciled_at_ns':now_ns,
        'audits':[[audit['sequence'],encode(audit)[1]] for audit in audits[-2:]],
        'account_event_time_ns':last['account']['event_time_ns'],'result':last['result'],
        'authorization':False,'real_book_events_created':False,'causal_proof':False}


def audit_mainnet_bracket(vault,parent: str,*,client_factory:Callable[[],GrvtMainnetClient],
        require_permission:Callable[[],None],clock_ns:Callable[[],int]=time.time_ns) -> dict:
    generation=vault.session_generation
    if generation is None:raise GrvtMainnetError('Bóveda bloqueada')
    last=stamp(clock_ns());start_call=last
    def checked():
        nonlocal last
        require_permission();at=stamp(clock_ns())
        if vault.session_generation!=generation or not last<=at<=start_call+120_000_000_000:
            raise GrvtMainnetError('Auditoría MAINNET revocada o vencida')
        last=at;return at
    checked();saved=read_mainnet_bracket(vault,parent)
    if saved.get('reconciliation') is not None:raise GrvtMainnetError('Grupo MAINNET ya conciliado')
    recovery_event=None
    if saved.get('recovery') and saved['recovery']['reductions']:
        from app.live_execution.grvt_mainnet_recovery import audit_recovery_position
        recovery_event=audit_recovery_position(vault,parent,client_factory=client_factory,require_permission=checked,clock_ns=checked)
        observed=read_mainnet_bracket(vault,parent)['last_observation']
    else:observed=observe_mainnet_bracket(vault,parent,client_factory=client_factory,require_permission=checked,clock_ns=checked)
    claim=saved['claim'];sub=saved['sub_account_id']
    try:
        recovery_fields={}
        if recovery_event is not None:
            from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
            from app.live_execution.grvt_mainnet_reduction import snapshot_reductions
            view=read_mainnet_recovery(vault,parent);details=recovery_event['details']
            templates,orders=snapshot_reductions(view,recovery_event)
            cutoff=details['cutoff_ns'];at=details['observed_at_ns'];histories=details['histories'];account=details['account']
            result=calculate_recovered_flat(claim,observed,histories,account,cutoff_ns=cutoff,observed_at_ns=at,
                extra_orders=orders,extra_templates=templates)
            recovery_fields={'recovery_snapshot_sequence':recovery_event['sequence'],'recovery_snapshot_digest':encode(recovery_event)[1],
                'reductions':[{key:item for key,item in state.items() if key!='observed_at_ns'} for state in orders]}
        else:
            with vault.session() as db:salt=_control(db,sub).identity_salt
            client=client_factory();checked()
            if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET' or client.sub_account_id!=sub
                    or client.identity_ref(salt)!=claim['account_ref']):raise ValueError('financial account identity')
            cutoff=checked();start=claim['preflight']['states'][-1]['observed_at_ns']
            histories=_histories(client,sub,start,cutoff,checked)
            account=_state(client,sub,checked);at=checked()
            result=calculate_native_flat(claim,observed,histories,account,cutoff_ns=cutoff,observed_at_ns=at)
        with vault.guard_session_generation(generation),vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE');finance_schema(db,required=True)
                from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
                row=db.get(GrvtMainnetBracket,parent);current=verified_mainnet_bracket(db,row)
                if current['last_observation']!=observed or row.payload_digest!=encode(claim)[1] or current.get('reconciliation'):
                    raise ValueError('financial source changed')
                head=db.get(GrvtMainnetAuditHead,parent)
                value={'schema':'orbis.grvt.mainnet.bracket.audit.v2' if recovery_event is not None else 'orbis.grvt.mainnet.bracket.audit.v1','network':'MAINNET','parent_client_order_id':parent,
                    'claim_digest':row.payload_digest,'sequence':head.count+1 if head else 1,'previous_digest':head.digest if head else None,
                    'observation_sequence':observed['sequence'],'observation_digest':encode(observed)[1],
                    'cutoff_ns':cutoff,'observed_at_ns':at,'histories':histories,'account':account,'result':result,**recovery_fields}
                text,digest=encode(value)
                db.add(GrvtMainnetAudit(parent_client_order_id=parent,sequence=value['sequence'],payload_json=text,payload_digest=digest))
                if head:head.count+=1;head.digest=digest
                else:db.add(GrvtMainnetAuditHead(parent_client_order_id=parent,count=1,digest=digest))
                checked();db.flush();verified_mainnet_bracket(db,row);db.commit();return value
            except Exception:db.rollback();raise
    except Exception:
        raise GrvtMainnetError('Conciliación MAINNET no verificable; conserva la reserva y revisa la cuenta') from None


def reconcile_mainnet_bracket(vault,parent: str,*,now_ns: int) -> dict:
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE');finance_schema(db,required=True)
            from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
            from app.live_execution.grvt_mainnet_observation import verified_mainnet_observations
            row=db.get(GrvtMainnetBracket,parent);saved=verified_mainnet_bracket(db,row)
            if saved.get('reconciliation') is not None:return saved['reconciliation']
            observations=verified_mainnet_observations(db,row,saved['claim'],saved['acknowledgement'])
            audits,_=verified_financial_evidence(db,row,saved['claim'],observations)
            payload=_receipt(row,saved['claim'],observations,audits,now_ns=now_ns)
            chain=verified_settlement_chain(db,row.sub_account_id)
            previous=chain[-1] if chain else None
            if previous and saved['claim']['claimed_at_ns']<previous['reconciled_at_ns']:
                raise ValueError('entry predates previous settlement')
            payload.update(sub_sequence=len(chain)+1,
                previous_parent_client_order_id=previous['parent_client_order_id'] if previous else None,
                previous_receipt_digest=encode(previous)[1] if previous else None)
            text,digest=encode(payload)
            db.add(GrvtMainnetReconciliation(parent_client_order_id=parent,payload_json=text,payload_digest=digest))
            head=db.get(GrvtMainnetSettlementControl,row.sub_account_id)
            if head:head.count+=1;head.parent_client_order_id=parent;head.digest=digest
            else:db.add(GrvtMainnetSettlementControl(sub_account_id=row.sub_account_id,
                parent_client_order_id=parent,count=1,digest=digest))
            control=_control(db,row.sub_account_id)
            if control.pending_parent_id!=parent:raise ValueError('pending entry changed')
            row.active_slot=None;control.pending_parent_id=None;control.revision+=1
            db.flush();verified_mainnet_bracket(db,row);db.commit();return payload
        except GrvtMainnetError:db.rollback();raise
        except Exception:
            db.rollback();raise GrvtMainnetError('No se pudo verificar el recibo financiero MAINNET') from None
