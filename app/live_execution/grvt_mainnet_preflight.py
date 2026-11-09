"""Entrada MAINNET aislada y conservadora; no acredita ejecución ni liquidación."""

from collections.abc import Callable
from decimal import Decimal,localcontext
import re

from app.derivatives.paper_math import MarginRules
from app.live_execution.grvt_margin_response import margin_rules_response
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError,MainnetBracketDraft
from app.live_execution.grvt_mainnet_transport import (GrvtMainnetClient,INSTRUMENT_URL,MARGIN_URL,TICKER_URL,_result)


def number(raw: object) -> Decimal:
    if not isinstance(raw,str) or not re.fullmatch(r'-?[0-9]{1,30}(?:\.[0-9]{1,18})?',raw): raise ValueError('decimal text')
    return Decimal(raw)


def canonical(value: Decimal) -> str:
    text=format(value,'f')
    return '0' if value==0 else text.rstrip('0').rstrip('.') if '.' in text else text


def _state(client: GrvtMainnetClient,sub: str,clock: Callable,*,allow_position: str|None=None) -> dict:
    raw=client.read_account('account_summary',sub)['result']
    orders=client.read_account('open_orders',sub)['result']; at=clock()
    if type(orders) is not list or orders:
        raise ValueError('non-isolated account')
    return account_state(raw,sub,at,allow_position=allow_position,open_order_count=0)


def account_state(raw: dict,sub: str,at: int,*,allow_position: str|None,open_order_count: int) -> dict:
    if (not isinstance(raw,dict) or raw.get('sub_account_id')!=sub
            or raw.get('settle_currency')!='USDT' or raw.get('margin_type')!='SIMPLE_CROSS_MARGIN'
            or raw.get('sub_account_mode')!='SINGLE_ASSET_MODE' or type(raw.get('positions')) is not list
            or len(raw['positions'])>1 or raw['positions'] and allow_position is None
            or not isinstance(raw.get('spot_balances'),list) or not 1<=len(raw['spot_balances'])<=100
            or not isinstance(raw.get('event_time'),str) or not re.fullmatch(r'[0-9]{1,20}',raw['event_time'])):
        raise ValueError('non-isolated account')
    balances={}
    for row in raw['spot_balances']:
        if (not isinstance(row,dict) or not isinstance(row.get('currency'),str)
                or not re.fullmatch(r'[A-Z0-9]{2,24}',row['currency']) or row['currency'] in balances):
            raise ValueError('balances')
        balances[row['currency']]=number(row['balance'])
    if 'USDT' not in balances or any(value!=0 for key,value in balances.items() if key!='USDT'):
        raise ValueError('other collateral')
    positions=[]
    for row in raw['positions']:
        if (not isinstance(row,dict) or row.get('sub_account_id')!=sub or row.get('instrument')!=allow_position
                or number(row['size'])==0):raise ValueError('unexpected position')
        positions.append([allow_position,canonical(number(row['size']))])
    values={key:canonical(number(raw[key])) for key in ('total_equity','initial_margin','maintenance_margin','unrealized_pnl')}
    return {'sub_account_id':sub,'event_time_ns':int(raw['event_time']),'observed_at_ns':at,
        'margin_type':raw['margin_type'],'sub_account_mode':raw['sub_account_mode'],'settle_currency':'USDT',
        'positions':positions,'open_order_count':open_order_count,'other_collateral':[],
        'settle_balance':canonical(balances['USDT']),**values}


def collect_mainnet_preflight(client: GrvtMainnetClient,draft: MainnetBracketDraft,*,public_post: Callable,clock_ns: Callable) -> dict:
    try:
        states=[]; rules=[]
        for _ in range(2):
            states.append(_state(client,draft.sub_account_id,clock_ns))
            raw=margin_rules_response(public_post(MARGIN_URL,{'instrument':draft.instrument}))
            fetched=clock_ns()
            rules.append(MarginRules.from_payload({**raw,'fetched_at_ns':fetched},draft.instrument,fetched).json())
        raw=_result(public_post(TICKER_URL,{'instrument':draft.instrument}))['result']; at=clock_ns()
        if (not isinstance(raw,dict) or raw.get('instrument')!=draft.instrument
                or not isinstance(raw.get('event_time'),str) or not re.fullmatch(r'[0-9]{1,20}',raw['event_time'])):
            raise ValueError('ticker')
        quote={'instrument':draft.instrument,'event_time_ns':int(raw['event_time']),'observed_at_ns':at,
            **{name:canonical(number(raw[field])) for name,field in (('bid','best_bid_price'),('ask','best_ask_price'),
                ('bid_size','best_bid_size'),('ask_size','best_ask_size'))}}
        proof={'schema':'orbis.grvt.mainnet.preflight.v1','network':'MAINNET','instrument_source':INSTRUMENT_URL,
            'margin_source':MARGIN_URL,'quote_source':TICKER_URL,'states':states,'rules':rules,'quote':quote,'checked_at_ns':clock_ns()}
        verify_mainnet_preflight(proof,draft,now_ns=proof['checked_at_ns'])
        return proof
    except Exception:
        raise GrvtMainnetError('Cuenta, margen o precio MAINNET no verificables') from None


def verify_mainnet_preflight(proof: dict,draft: MainnetBracketDraft,*,now_ns: int) -> None:
    if type(proof) is dict and proof.get('schema')=='orbis.grvt.mainnet.perp.preflight.v1':
        from app.live_execution.grvt_perp_preflight import verify_perp_preflight
        verify_perp_preflight(proof,draft,now_ns=now_ns)
        return
    try:
        if (set(proof)!={'schema','network','instrument_source','margin_source','quote_source','states','rules','quote','checked_at_ns'}
                or proof['schema']!='orbis.grvt.mainnet.preflight.v1' or proof['network']!='MAINNET'
                or (proof['instrument_source'],proof['margin_source'],proof['quote_source'])!=(INSTRUMENT_URL,MARGIN_URL,TICKER_URL)
                or type(now_ns) is not int or not 0<now_ns<2**63
                or type(proof['checked_at_ns']) is not int or not 0<=now_ns-proof['checked_at_ns']<=5_000_000_000
                or not isinstance(proof['states'],list) or len(proof['states'])!=2
                or not isinstance(proof['rules'],list) or len(proof['rules'])!=2):
            raise ValueError('preflight scope')
        first,last=proof['states']; rule_values=[]
        with localcontext() as context:
            context.prec=100
            notional=draft.size*draft.entry_price
            for state in (first,last):
                if (set(state)!={'sub_account_id','event_time_ns','observed_at_ns','settle_balance','total_equity',
                        'initial_margin','maintenance_margin','unrealized_pnl','margin_type','sub_account_mode','settle_currency',
                        'positions','open_order_count','other_collateral'} or state['sub_account_id']!=draft.sub_account_id
                        or state['margin_type']!='SIMPLE_CROSS_MARGIN' or state['sub_account_mode']!='SINGLE_ASSET_MODE'
                        or state['settle_currency']!='USDT' or state['positions']!=[] or state['other_collateral']!=[]
                        or type(state['open_order_count']) is not int or state['open_order_count']!=0
                        or any(type(state[key]) is not int or not 0<state[key]<=now_ns+2_000_000_000
                            or now_ns-state[key]>5_000_000_000 for key in ('event_time_ns','observed_at_ns'))
                        or any(number(state[key])!=0 for key in ('initial_margin','maintenance_margin','unrealized_pnl'))
                        or number(state['settle_balance'])!=number(state['total_equity'])
                        or number(state['total_equity'])<notional*2):
                    raise ValueError('account risk')
            if (first['event_time_ns']>last['event_time_ns'] or first['observed_at_ns']>last['observed_at_ns']
                    or any(first[key]!=last[key] for key in ('settle_balance','total_equity','initial_margin','maintenance_margin','unrealized_pnl'))):
                raise ValueError('unstable account')
            for raw in proof['rules']:
                rule=MarginRules.from_payload(raw,draft.instrument,now_ns)
                if draft.size>rule.max_size or rule.tier_for(notional).max_leverage<1: raise ValueError('margin limit')
                rule_values.append(rule.json())
            if ({key:value for key,value in rule_values[0].items() if key!='fetched_at_ns'}
                    !={key:value for key,value in rule_values[1].items() if key!='fetched_at_ns'}):
                raise ValueError('changed margin rules')
            quote=proof['quote']
            if (set(quote)!={'instrument','event_time_ns','observed_at_ns','bid','ask','bid_size','ask_size'}
                    or quote['instrument']!=draft.instrument
                    or any(type(quote[key]) is not int or not 0<quote[key]<=now_ns+2_000_000_000
                        or now_ns-quote[key]>5_000_000_000 for key in ('event_time_ns','observed_at_ns'))):
                raise ValueError('quote age')
            timeline=(first['observed_at_ns'],proof['rules'][0]['fetched_at_ns'],last['observed_at_ns'],
                proof['rules'][1]['fetched_at_ns'],quote['observed_at_ns'],proof['checked_at_ns'],now_ns)
            if any(type(at) is not int for at in timeline) or list(timeline)!=sorted(timeline):
                raise ValueError('local observation after claim or regressed clock')
            bid,ask=number(quote['bid']),number(quote['ask'])
            reference=ask if draft.direction=='LONG' else bid
            liquidity=number(quote['ask_size' if draft.direction=='LONG' else 'bid_size'])
            if (not 0<bid<=ask or liquidity<draft.size or min(number(quote['bid_size']),number(quote['ask_size']))<=0
                    or (ask-bid)*10000/((bid+ask)/2)>draft.max_spread_bps
                    or abs(draft.entry_price-reference)*10000/reference>draft.max_slippage_bps
                    or reference*draft.size>draft.entry_cap):
                raise ValueError('price or liquidity bound')
    except Exception:
        raise GrvtMainnetError('Preflight MAINNET no reproducible o fuera de límites') from None
