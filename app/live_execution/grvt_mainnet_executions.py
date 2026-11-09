"""Fills para inspección gráfica: cálculos exactos y ninguna consulta de red."""
from datetime import datetime,timezone
from decimal import localcontext
from app.live_execution.grvt_mainnet_journal import encode,verified_mainnet_bracket
from app.live_execution.grvt_mainnet_bracket import client_ids,GrvtMainnetError
from app.live_execution.grvt_mainnet_preflight import number,canonical
from app.portfolio.models import GrvtMainnetBracket,GrvtMainnetBotSelection,GrvtMainnetBotRun


def fill_executions(rows:list[dict], roles:dict[str,str])->list[dict]:
    executions=[];seen=set()
    if len(rows)>10000:raise GrvtMainnetError('Demasiados fills para esta instantánea')
    with localcontext() as context:
        context.prec=100
        for fill in sorted(rows,key=lambda item:(item['event_time_ns'],item['trade_id'])):
            if fill['trade_id'] in seen or fill['client_order_id'] not in roles:raise GrvtMainnetError('Fills duplicados o ajenos al grupo')
            seen.add(fill['trade_id']);stamp=fill['event_time_ns'];qty=number(fill['size']);price=number(fill['price'])
            if qty<=0 or price<=0:raise GrvtMainnetError('Fill sin cantidad/precio representables')
            executions.append({'fill_id':fill['trade_id'],'time_ns':str(stamp),'time_ms':stamp//1_000_000,
                'occurred_at':datetime.fromtimestamp(stamp//1_000_000_000,timezone.utc).isoformat(),
                'side':'BUY' if fill['is_buyer'] else 'SELL','role':roles[fill['client_order_id']],
                'quantity_base':canonical(qty),'price':canonical(price),'gross_quote':canonical(qty*price),
                'fee':fill['fee'],'fee_asset':fill['fee_currency'],'client_order_id':fill['client_order_id'],'order_id':fill['order_id']})
    return executions


def executions_view(group:dict,*,timeframe:str='1h',entry:dict|None=None)->dict:
    parent=group['parent_client_order_id'];draft=group['claim']['draft'];rows=[];at=group['claim']['claimed_at_ns']
    if group['audits']:
        source=group['audits'][-1];rows=source['histories']['fill_history']['rows'];at=source['observed_at_ns']
    elif group['recovery'] and (snapshots:=[event for event in group['recovery']['events'] if event['type']=='REDUCTION_SNAPSHOT']):
        source=snapshots[-1]['details'];rows=source['histories']['fill_history']['rows'];at=source['observed_at_ns']
    elif entry is not None:
        rows=entry['fills']['rows'];at=entry['cutoff_ns']
    roles=dict(zip(client_ids(parent),('ENTRY','SL','TP')))
    if group['recovery']:
        roles.update((item['claim']['client_order_id'],'REDUCTION') for item in group['recovery']['reductions'])
    executions=fill_executions(rows,roles)
    receipt=group['reconciliation'];instrument=draft['instrument'];base=instrument.removesuffix('_USDT_Perp')
    result={'environment':'REAL','network':'MAINNET','provider':'grvt','parent_client_order_id':parent,'sub_account_id':group['sub_account_id'],
        'symbol':base+'/USDT:PERP','timeframe':timeframe,'base_asset':base,'quote_asset':'USDT',
        'evidence':'OWN_VERIFIED_FILLS' if rows else 'NO_VERIFIED_FILLS','as_of_ns':str(at),'authorization':False,
        'financially_reconciled':receipt is not None,'net_realized_pnl_usdt':receipt['result']['net_realized_pnl_usdt'] if receipt else None,
        'receipt_digest':encode(receipt)[1] if receipt else None,'executions':executions,'total':len(executions)}
    return {**result,'revision':encode(result)[1]}


def executions_page(value:dict,*,limit:int,offset:int,revision:str|None=None)->dict:
    if revision is not None and revision!=value['revision']:raise GrvtMainnetError('Los fills cambiaron; actualiza el gráfico')
    if offset>value['total']:raise GrvtMainnetError('Tramo de fills fuera de cobertura')
    end=min(offset+limit,value['total'])
    return {**value,'executions':value['executions'][offset:end],'offset':offset,'next_offset':end if end<value['total'] else None}


def read_executions_with_origin(db,parent:str)->tuple[dict,dict|None,str]:
    from app.live_execution.grvt_mainnet_entry_fills import read_entry_fills,entry_fills_from_exit_proof,verify_entry_fills
    from app.portfolio.models import GrvtMainnetBotExit
    from app.live_execution.grvt_mainnet_bot_exit import verify_exit_proof
    import json
    group=verified_mainnet_bracket(db,db.get(GrvtMainnetBracket,parent));entry=read_entry_fills(db,parent)
    if entry is None and (row:=db.get(GrvtMainnetBotExit,parent)) is not None:
        proof=json.loads(row.payload_json)
        if encode(proof)!=(row.payload_json,row.payload_digest):raise GrvtMainnetError('Salida archivada modificada')
        verify_exit_proof(db,proof);entry=verify_entry_fills(db,entry_fills_from_exit_proof(proof))
    timeframe='1h';grant=None;selection=db.get(GrvtMainnetBotSelection,parent)
    if selection is not None:
        from app.live_execution.grvt_mainnet_bot_entry import _read
        grant=_read(db,db.get(GrvtMainnetBotRun,selection.run_id),groups=False);timeframe=grant['source']['timeframe']
    return executions_view(group,timeframe=timeframe,entry=entry),grant,group['claim']['draft']['direction']


def read_executions(db,parent:str)->dict:
    return read_executions_with_origin(db,parent)[0]
