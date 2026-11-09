"""Salida MAINNET desde fills observados y mercado propio; no autoriza ejecución."""
from dataclasses import asdict
from decimal import Decimal,localcontext
import json,time
from app.live_execution.grvt_exit_rules import evaluate_verified_exit,GrvtExitMark,required_exit_warmup
from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError,exact_uint
from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket,encode,_instrument
from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient
from app.live_execution.grvt_mainnet_finance import _fill,_ID,_FILL_FIELDS
from app.live_execution.grvt_mainnet_preflight import number
from app.live_execution.grvt_mainnet_exit_market import fetch_mainnet_exit_market
from app.lab.engine import INTERVALS


def entry_from_fills(claim,observation,section,*,cutoff_ns):
    state=observation['state'];entry=state['orders'][0];draft=claim['draft'];instrument=_instrument(claim['instrument'])
    if (entry['status']!='FILLED' or number(entry['traded_size'])!=number(entry['size'])
            or any(number(row['traded_size'])!=0 for row in state['orders'][1:])
            or set(section)!={'pages','drained','rows'} or section['drained'] is not True
            or type(section['pages']) is not int or not 1<=section['pages']<=20
            or not isinstance(section['rows'],list) or not 1<=len(section['rows'])<=500*section['pages']):
        raise GrvtMainnetError('Entrada MAINNET sin fills completos verificables')
    ids=set();last=0;size=Decimal(0);notional=Decimal(0)
    with localcontext() as context:
        context.prec=100
        for fill in section['rows']:
            if (set(fill)!=_FILL_FIELDS or not isinstance(fill['trade_id'],str) or not _ID.fullmatch(fill['trade_id'])
                    or fill['trade_id'] in ids or fill['client_order_id']!=entry['client_order_id'] or fill['order_id']!=entry['order_id']
                    or fill['instrument']!=draft['instrument'] or type(fill['is_buyer']) is not bool
                    or fill['is_buyer'] is not (draft['direction']=='LONG') or type(fill['is_taker']) is not bool
                    or fill['is_liquidation'] is not False or fill['fee_currency']!='USDT' or number(fill['builder_fee'])!=0
                    or number(fill['realized_pnl'])!=0 or type(fill['event_time_ns']) is not int
                    or not int(entry['create_time_ns'])<=fill['event_time_ns']<=int(entry['update_time_ns'])
                    or fill['event_time_ns']>cutoff_ns):raise GrvtMainnetError('Fill ajeno o contradictorio en entrada MAINNET')
            qty=number(fill['size']);price=number(fill['price'])
            if qty<=0 or price<=0:raise GrvtMainnetError('Fill MAINNET sin cantidad o precio positivos')
            number(fill['fee'])
            exact_uint(qty,instrument.base_decimals,2**64-1);exact_uint(price,9,2**64-1)
            if price>number(draft['entry_price']) if fill['is_buyer'] else price<number(draft['entry_price']):
                raise GrvtMainnetError('Fill fuera del límite firmado MAINNET')
            ids.add(fill['trade_id']);last=max(last,fill['event_time_ns']);size+=qty;notional+=qty*price
        if size!=number(entry['traded_size']):raise GrvtMainnetError('Cantidad de fills MAINNET incompleta')
    return size,notional,last


def review_mainnet_bot_exit(vault,run_id,parent,*,client_factory,public_post,require_permission,clock_ns=time.time_ns,market_cache=None):
    generation=vault.session_generation;start=clock_ns();last=start
    def checked():
        nonlocal last
        require_permission();at=clock_ns()
        if (generation is None or vault.session_generation!=generation or type(at) is not int
                or not 0<last<=at<=start+120_000_000_000):raise GrvtMainnetError('Revisión de salida MAINNET revocada o vencida')
        last=at;return at
    def public(url,body):checked();reply=public_post(url,body);checked();return reply
    checked();grant=read_bot_grant(vault,run_id)
    if not grant['selections'] or grant['selections'][-1]['parent_client_order_id']!=parent or grant['terminal'] is not None:
        raise GrvtMainnetError('La entrada no corresponde al bot MAINNET activo')
    observed=observe_mainnet_bracket(vault,parent,client_factory=client_factory,require_permission=checked,clock_ns=checked)
    saved=read_mainnet_bracket(vault,parent);client=client_factory();checked()
    from app.live_execution.grvt_mainnet_journal import _control
    with vault.session() as db:salt=_control(db,saved['sub_account_id']).identity_salt
    if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET' or client.sub_account_id!=saved['sub_account_id']
            or client.identity_ref(salt)!=saved['claim']['account_ref']):raise GrvtMainnetError('Identidad MAINNET de fills cambiada')
    cursor='';seen=set();rows=[];cutoff=checked()
    for page in range(1,21):
        checked();batch=client.history_page('fill_history',saved['sub_account_id'],
            start_ns=saved['claim']['preflight']['states'][-1]['observed_at_ns'],end_ns=cutoff,cursor=cursor);checked()
        if (not isinstance(batch.get('result'),list) or len(batch['result'])>500 or not isinstance(batch.get('next'),str)
                or len(batch['next'])>1024 or not batch['next'].isascii() or any(ord(c)<32 for c in batch['next'])):
            raise GrvtMainnetError('Historial de fills MAINNET incompleto')
        rows.extend(_fill(row,saved['sub_account_id']) for row in batch['result']);following=batch['next']
        if not following:break
        if following in seen:raise GrvtMainnetError('Paginación de fills MAINNET repetida')
        seen.add(following);cursor=following
    else:raise GrvtMainnetError('Historial de fills MAINNET excede la cobertura')
    section={'pages':page,'drained':True,'rows':rows}
    size,notional,last_fill=entry_from_fills(saved['claim'],observed,section,cutoff_ns=cutoff)
    source=grant['source'];step=INTERVALS[source['timeframe']]
    warmup=max(500,required_exit_warmup(source['definition']['exit'],step) if 'exit' in source['definition'] else 0)
    history_start=max(step,(saved['claim']['issued_at_ns']//1_000_000_000//step-warmup)*step)
    market=fetch_mainnet_exit_market(source['symbol'],source['timeframe'],history_start_t=history_start,public_post=public,clock_ns=checked,previous=market_cache.get('value') if market_cache is not None else None)
    if market_cache is not None:market_cache['value']=market
    decision=evaluate_verified_exit(source['definition'],timeframe=source['timeframe'],engine_version=source['engine_version'],
        instrument=source['instrument'],size=size,notional=notional,last_fill=last_fill,network='MAINNET',
        candles=market.candles,mark=market.mark,now_ns=checked())
    proof={'schema':'orbis.grvt.mainnet.bot.exit.v1','network':'MAINNET','run_id':run_id,'parent_client_order_id':parent,
        'claim_digest':encode(saved['claim'])[1],'observation_sequence':observed['sequence'],'observation_digest':encode(observed)[1],
        'cutoff_ns':cutoff,'fills':section,'market':{'mark':asdict(market.mark),'candles':[bar.json() for bar in market.candles],
            'history_start_t':history_start,'history_end_t':market.history_end_t,'history_digest':market.history_digest},
        'evaluated_at_ns':checked(),'decision':asdict(decision),'authorization':False}
    return json.loads(encode(proof)[0])


def verify_exit_proof(db,proof):
    from app.live_execution.grvt_mainnet_bot_entry import _read
    from app.portfolio.models import GrvtMainnetBotRun,GrvtMainnetBracket,GrvtMainnetObservation
    from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
    from app.lab.engine import validate_candles
    if (set(proof)!={'schema','network','run_id','parent_client_order_id','claim_digest','observation_sequence','observation_digest',
            'cutoff_ns','fills','market','evaluated_at_ns','decision','authorization'}
            or proof['schema']!='orbis.grvt.mainnet.bot.exit.v1' or proof['network']!='MAINNET' or proof['authorization'] is not False):
        raise GrvtMainnetError('Prueba de salida MAINNET no válida')
    parent=proof['parent_client_order_id'];grant=_read(db,db.get(GrvtMainnetBotRun,proof['run_id']),groups=False)
    if not any(item['parent_client_order_id']==parent for item in grant['selections']):raise GrvtMainnetError('Salida no vinculada a la concesión')
    group=verified_mainnet_bracket(db,db.get(GrvtMainnetBracket,parent));source=grant['source']
    row=db.get(GrvtMainnetObservation,(parent,proof['observation_sequence']))
    if (row is None or row.payload_digest!=proof['observation_digest'] or encode(group['claim'])[1]!=proof['claim_digest']):
        raise GrvtMainnetError('Observación de salida MAINNET modificada')
    observed=json.loads(row.payload_json);cutoff=proof['cutoff_ns'];at=proof['evaluated_at_ns']
    if type(cutoff) is not int or type(at) is not int or not observed['state']['observed_at_ns']<=cutoff<=at<=cutoff+120_000_000_000:
        raise GrvtMainnetError('Cronología de salida MAINNET inválida')
    size,notional,last=entry_from_fills(group['claim'],observed,proof['fills'],cutoff_ns=cutoff)
    market=proof['market'];step=INTERVALS[source['timeframe']]
    warmup=max(500,required_exit_warmup(source['definition']['exit'],step) if 'exit' in source['definition'] else 0)
    start=max(step,(group['claim']['issued_at_ns']//1_000_000_000//step-warmup)*step)
    if set(market)!={'mark','candles','history_start_t','history_end_t','history_digest'}:raise ValueError('exit market shape')
    raw=market['mark']
    if set(raw)!={'instrument','event_time_ns','observed_at_ns','price'}:raise ValueError('mark shape')
    mark=GrvtExitMark(raw['instrument'],raw['event_time_ns'],raw['observed_at_ns'],number(raw['price']))
    end=mark.event_time_ns//1_000_000_000//step*step
    bars,gaps=validate_candles(market['candles'],source['timeframe'],now=end)
    digest=encode({'source':'GRVT_MAINNET_TRADE_AND_MARK','symbol':source['symbol'],'timeframe':source['timeframe'],
        'start_t':start,'end_t':end,'candles':[bar.json() for bar in bars]})[1]
    if (gaps or not bars or bars[0].t!=start or market['history_start_t']!=start or market['history_end_t']!=end
            or market['history_digest']!=digest):raise GrvtMainnetError('Histórico de salida MAINNET incompleto o modificado')
    decision=evaluate_verified_exit(source['definition'],timeframe=source['timeframe'],engine_version=source['engine_version'],
        instrument=source['instrument'],size=size,notional=notional,last_fill=last,network='MAINNET',candles=bars,mark=mark,now_ns=at)
    if encode(asdict(decision))!=encode(proof['decision']) or decision.action!='CLOSE':raise GrvtMainnetError('Salida MAINNET no reproducible')
    return proof


def save_bot_exit(vault,proof,*,require_permission):
    from app.portfolio.models import GrvtMainnetBotExit
    generation=vault.session_generation;require_permission()
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE');verify_exit_proof(db,proof)
            parent=proof['parent_client_order_id'];row=db.get(GrvtMainnetBotExit,parent);text,digest=encode(proof)
            if row is not None:
                if (row.payload_json,row.payload_digest)!=(text,digest):raise ValueError('different exit proof')
            else:db.add(GrvtMainnetBotExit(parent_client_order_id=parent,payload_json=text,payload_digest=digest))
            require_permission();db.flush();db.commit();return proof
        except Exception:
            db.rollback();raise GrvtMainnetError('No se pudo conservar la decisión de salida MAINNET') from None


def verify_all_bot_exits(db):
    from app.portfolio.models import GrvtMainnetBotExit
    for row in db.query(GrvtMainnetBotExit):
        proof=json.loads(row.payload_json)
        if proof['parent_client_order_id']!=row.parent_client_order_id or encode(proof)!=(row.payload_json,row.payload_digest):
            raise GrvtMainnetError('Registro de salida MAINNET truncado o modificado')
        verify_exit_proof(db,proof)
