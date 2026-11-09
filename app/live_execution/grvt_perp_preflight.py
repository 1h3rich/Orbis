"""Fresh private account/configuration and public rules before a profile entry.

Separate from published preflight v1. All calculations are replayed from
inputs. This module does not grant authority, alter configuration or send orders.
"""
from __future__ import annotations

from collections.abc import Callable
import re

from app.derivatives.perp_observed_wire import (
    ObservedRiskSnapshot,ObservedSpecification,QuoteInput,rules_input,specification_input,
)
from app.derivatives.paper_math import InstrumentSpec,MarginRules
from app.integrations.grvt_position_config import PositionConfiguration,POSITION_CONFIG_URL,leverage,verify_configuration_pair
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError,MainnetBracketDraft
from app.live_execution.grvt_mainnet_preflight import _state,number
from app.live_execution.grvt_mainnet_transport import (
    GrvtMainnetClient,INSTRUMENT_URL,MARGIN_URL,TICKER_URL,_result,fetch_mainnet_instrument,
)
from app.live_execution.grvt_margin_response import margin_rules_response
from app.live_execution.grvt_perp_plan import (
    FRESH_NS,PerpEntryTerms,PerpEntryPlan,PerpInstrument,PerpMarketInput,PerpPlanInput,PerpRealBudget,
    build_perp_plan,instrument_input,plan_draft,
)


SCHEMA='orbis.grvt.mainnet.perp.preflight.v1'
SOURCES={'instrument':INSTRUMENT_URL,'margin':MARGIN_URL,'quote':TICKER_URL,'configuration':POSITION_CONFIG_URL}


def _instrument_snapshot(name: str,post: Callable,clock: Callable) -> dict:
    reply=post(INSTRUMENT_URL,{'instrument':name})
    instrument=fetch_mainnet_instrument(name,http_post=lambda url,body:reply)
    row=_result(reply)['result'];at=clock()
    spec=InstrumentSpec.from_payload({**row,'fetched_at_ns':at},name,at)
    return {'instrument':instrument_input(instrument).payload(),'specification':specification_input(spec).payload()}


MARKET_FIELDS = {'states', 'configurations', 'instruments', 'rules', 'quote', 'checked_at_ns'}


def collect_perp_market(client: GrvtMainnetClient, instrument: str, sub_account_id: str, *,
                        public_post: Callable, clock_ns: Callable) -> dict:
    def post(url,body):
        clock_ns();reply=public_post(url,body);clock_ns();return reply
    class ReadScope:
        def read_account(self,path,sub):
            clock_ns();reply=client.read_account(path,sub);clock_ns();return reply
    states=[];configs=[];instruments=[];rules=[]
    for _ in range(2):
        clock_ns()
        configs.append(client.position_configuration(sub_account_id,instrument).payload())
        clock_ns()
        states.append(_state(ReadScope(),sub_account_id,clock_ns))
        instruments.append(_instrument_snapshot(instrument,post,clock_ns))
        raw=margin_rules_response(post(MARGIN_URL,{'instrument':instrument}));at=clock_ns()
        current=MarginRules.from_payload({**raw,'fetched_at_ns':at},instrument,at)
        rules.append(rules_input(current).model_dump())
    raw=_result(post(TICKER_URL,{'instrument':instrument}))['result'];at=clock_ns()
    quote={'quote':QuoteInput.model_validate({
        'instrument':raw['instrument'],'event_time_ns':raw['event_time'],
        **{key:raw[key] for key in ('mark_price',)},'bid':raw['best_bid_price'],'ask':raw['best_ask_price'],
    }).payload(),'bid_size':raw['best_bid_size'],'ask_size':raw['best_ask_size'],'observed_at_ns':str(at)}
    checked=clock_ns()
    return {'states':states,'configurations':configs,'instruments':instruments,
            'rules':rules,'quote':quote,'checked_at_ns':checked}


def verify_perp_market(proof: dict, *, instrument: str, margin_mode: str,
                       leverage_value: str, budget: PerpRealBudget, now_ns: int) -> PerpMarketInput:
    if (type(proof) is not dict or set(proof) != MARKET_FIELDS
            or type(now_ns) is not int or not 0 < now_ns < 2**63
            or type(proof['checked_at_ns']) is not int
            or not 0 <= now_ns - proof['checked_at_ns'] <= FRESH_NS
            or any(type(proof[key]) is not list or len(proof[key]) != 2
                   for key in ('states', 'configurations', 'instruments', 'rules'))):
        raise ValueError('Observaciones de mercado REAL incompletas o antiguas')
    configs=[PositionConfiguration.model_validate(raw) for raw in proof['configurations']]
    verify_configuration_pair(*configs,now_ns=now_ns)
    if any(config.sub_account_id!=budget.sub_account_id or config.instrument!=instrument
            or config.margin_type!=margin_mode or leverage(config.leverage)!=leverage(leverage_value)
            for config in configs):
        raise ValueError('profile config mismatch')
    first,last=proof['states']
    for state in (first,last):
        if (set(state)!={'sub_account_id','event_time_ns','observed_at_ns','settle_balance','total_equity',
                'initial_margin','maintenance_margin','unrealized_pnl','margin_type','sub_account_mode','settle_currency',
                'positions','open_order_count','other_collateral'}
                or state['sub_account_id']!=budget.sub_account_id
                or state['margin_type']!='SIMPLE_CROSS_MARGIN' or state['sub_account_mode']!='SINGLE_ASSET_MODE'
                or state['settle_currency']!='USDT' or state['positions']!=[] or state['other_collateral']!=[]
                or type(state['open_order_count']) is not int or state['open_order_count']!=0
                or any(type(state[key]) is not int or not 0<=now_ns-state[key]<=FRESH_NS
                    for key in ('event_time_ns','observed_at_ns'))
                or any(number(state[key])!=0 for key in ('initial_margin','maintenance_margin','unrealized_pnl'))
                or number(state['settle_balance'])!=number(state['total_equity'])
                or not 0<number(state['total_equity'])<=number(budget.collateral_limit)):
            raise ValueError('profile account risk')
    if (first['event_time_ns']>last['event_time_ns']
            or any(first[key]!=last[key] for key in ('settle_balance','total_equity','initial_margin','maintenance_margin','unrealized_pnl'))):
        raise ValueError('profile account changed')
    insts=[];specs=[];rule_values=[]
    for raw in proof['instruments']:
        if type(raw) is not dict or set(raw)!={'instrument','specification'}:
            raise ValueError('instrument snapshot shape')
        insts.append(PerpInstrument.model_validate(raw['instrument']))
        specs.append(ObservedSpecification.model_validate(raw['specification']))
    for raw in proof['rules']:rule_values.append(ObservedRiskSnapshot.model_validate(raw))
    if (insts[0].payload()!=insts[1].payload()
            or specs[0].model_dump(exclude={'fetched_at_ns'})!=specs[1].model_dump(exclude={'fetched_at_ns'})
            or rule_values[0].model_dump(exclude={'fetched_at_ns'})!=rule_values[1].model_dump(exclude={'fetched_at_ns'})):
        raise ValueError('profile metadata changed')
    quote=proof['quote']
    if type(quote) is not dict or set(quote)!={'quote','bid_size','ask_size','observed_at_ns'}:
        raise ValueError('profile quote shape')
    q=QuoteInput.model_validate(quote['quote'])
    if (type(quote['observed_at_ns']) is not str or not re.fullmatch(r'[1-9][0-9]{0,18}',quote['observed_at_ns'])
            or min(number(quote['bid_size']),number(quote['ask_size']))<=0):
        raise ValueError('profile liquidity')
    timeline=[]
    for index in range(2):
        timeline.extend([int(configs[index].requested_at_ns),int(configs[index].received_at_ns),
            proof['states'][index]['observed_at_ns'],int(specs[index].fetched_at_ns),int(rule_values[index].fetched_at_ns)])
    timeline.extend([int(quote['observed_at_ns']),proof['checked_at_ns'],now_ns])
    if (any(type(at) is not int for at in timeline) or timeline!=sorted(timeline)
            or now_ns-timeline[0]>FRESH_NS):
        raise ValueError('profile observation chronology')
    return PerpMarketInput(instrument=insts[-1], specification=specs[-1], rules=rule_values[-1],
                           quote=q, account_equity=last['total_equity'], checked_at_ns=str(proof['checked_at_ns']))


def collect_perp_preflight(client: GrvtMainnetClient,terms: PerpEntryTerms,*,
                           public_post: Callable,clock_ns: Callable) -> dict:
    try:
        terms = PerpEntryTerms.model_validate(terms.payload())
        captured = collect_perp_market(client, terms.instrument, terms.budget.sub_account_id,
                                       public_post=public_post, clock_ns=clock_ns)
        at = captured['checked_at_ns']
        market = verify_perp_market(captured, instrument=terms.instrument, margin_mode=terms.profile.margin_mode,
                                    leverage_value=terms.profile.leverage, budget=terms.budget, now_ns=at)
        plan = build_perp_plan(PerpPlanInput.model_validate({**market.payload(), 'terms':terms.payload()}))
        proof = {'schema':SCHEMA,'network':'MAINNET','sources':dict(SOURCES),'terms':terms.payload(),
                 **captured, 'plan':plan.payload()}
        verify_perp_preflight(proof, plan_draft(plan), now_ns=at)
        return proof
    except Exception:
        raise GrvtMainnetError('Preflight del perfil MAINNET no verificable o fuera de presupuesto') from None


def verify_perp_preflight(proof: dict,draft: MainnetBracketDraft,*,now_ns: int) -> None:
    try:
        if (type(proof) is not dict or set(proof) != MARKET_FIELDS | {'schema','network','sources','terms','plan'}
                or proof['schema'] != SCHEMA or proof['network'] != 'MAINNET' or proof['sources'] != SOURCES):
            raise ValueError('profile preflight shape')
        terms = PerpEntryTerms.model_validate(proof['terms'])
        market = verify_perp_market({key:proof[key] for key in MARKET_FIELDS}, instrument=draft.instrument,
                                    margin_mode=terms.profile.margin_mode, leverage_value=terms.profile.leverage,
                                    budget=terms.budget, now_ns=now_ns)
        quote = proof['quote']
        if number(quote['ask_size'] if draft.direction == 'LONG' else quote['bid_size']) < draft.size:
            raise ValueError('profile liquidity')
        plan = PerpEntryPlan.model_validate(proof['plan'])
        expected = PerpPlanInput.model_validate({**market.payload(), 'terms':terms.payload()})
        if plan.source.payload()!=expected.payload() or plan_draft(plan)!=draft:
            raise ValueError('profile plan not bound to observations/draft')
        # Recheck age at transmission, not only at the earlier collection time.
        checked=expected.payload();checked['checked_at_ns']=str(now_ns)
        from app.live_execution.grvt_perp_plan import calculate_perp_plan
        if calculate_perp_plan(PerpPlanInput.model_validate(checked))!=plan.result:
            raise ValueError('profile plan changed')
    except Exception:
        raise GrvtMainnetError('Preflight del perfil MAINNET no reproducible o vencido') from None
