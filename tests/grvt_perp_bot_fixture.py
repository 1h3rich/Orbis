"""Owned encrypted LAB/PAPER/REAL and independent public/private venue I/O."""
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.derivatives.perp_observed_wire import CloseEvent, observation_input
from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy
from app.live_execution.grvt_perp_bot_grant import (
    PerpBotSelectionRequest, _signal_selection, append_perp_market, begin_perp_bot_grant,
)
from app.live_execution.grvt_perp_bot_origin import verified_perp_bot_origin
from app.live_execution.grvt_perp_plan import PerpEntryTerms
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import record_signal_input
from app.live_execution.grvt_perp_submit import submit_perp_entry_once
from tests.grvt_perp_mainnet_fixture import perp_case
from tests.perp_observed_archive_fixture import NS, owned_archive
from tests.perp_observed_fixture import bar, batch
from tests.perp_observed_source_fixture import OwnedPublicAdapter
from tests.test_grvt_mainnet_submit import arguments


def perp_bot_case(directory,monkeypatch,*,direction='LONG',mode='ISOLATED',policy_changes=None,supervised=False,create_grant=True,optimized=False):
    source_dir=directory/'source'
    source_dir.mkdir(parents=True)
    lab,paper,archive,old_origin,old_seed,clock=owned_archive(source_dir,monkeypatch,
        direction=direction,margin_mode=mode,optimized=optimized)
    session=archive.create(old_origin,old_seed,request_id=str(uuid4()))['id']
    initial=old_seed.start().state
    for seconds in range(10,61,10):
        clock['now']=initial.started_at_ns+seconds*NS
        archive.append(session,observation_input(batch(initial,seconds,
            bars=(bar(initial.started_at_ns//NS,direction),) if seconds==60 else ())),request_id=str(uuid4()))
    clock['now']+=NS
    archive.append(session,CloseEvent(schema_name='orbis.paper.perp.observed.input.v1',
        kind='CLOSE_REQUEST',received_at_ns=str(clock['now'])),request_id=str(uuid4()))
    clock['now']=initial.started_at_ns+70*NS
    archive.append(session,observation_input(batch(archive.current_state(session),70,
        bid='103' if direction=='LONG' else '95',ask='104' if direction=='LONG' else '96')),
        request_id=str(uuid4()))
    case=perp_case(directory/'real',mode=mode,direction=direction)
    case.clock[0]=clock['now']
    origin=verified_perp_bot_origin(lab,archive,session,'123456',case.factory(),clock_ns=lambda:clock['now'])
    adapter=OwnedPublicAdapter(old_seed,clock)
    adapter.rows[initial.started_at_ns//NS]=bar(initial.started_at_ns//NS,direction).json()
    source=GrvtPerpSignalSource(adapter=adapter,clock_ns=lambda:clock['now'])
    seed=source.prepare(origin.origin)
    raw=case.terms.payload()
    raw.update(profile=seed.profile.payload(),definition=seed.definition,timeframe=seed.timeframe)
    terms=PerpEntryTerms.model_validate(raw)
    policy=MainnetBotPolicy('123456',Decimal(terms.budget.entry_notional_limit),
        Decimal(terms.budget.exit_notional_limit),Decimal('2000'),Decimal('100'),max_entries=2,
        wait_seconds=3600,exit_seconds=terms.budget.exit_seconds,
        max_spread_bps=terms.budget.max_spread_bps,max_slippage_bps=terms.budget.max_slippage_bps)
    if policy_changes:
        policy=replace(policy,**policy_changes)
    grant=(begin_perp_bot_grant(case.vault,origin,seed,terms,policy,now_ns=clock['now'],keep_operating=True,
        supervised=supervised) if create_grant else None)
    state=seed.start()
    records=[]
    def record_quote(at=None):
        nonlocal state
        if at is None:
            # A private audit may have advanced time. Record only the current
            # point; never fill the unseen interval with synthetic quotes.
            if clock['now']<=state.last_accepted_ns:
                next_close=(state.history[-1].t+120)*NS
                clock['now']+=min(10*NS,max(1,next_close-clock['now']))
        else:clock['now']=at
        case.clock[0]=clock['now']
        end=clock['now']//(60*NS)*60
        stamp=max(adapter.rows)+60
        while stamp<end:
            adapter.rows[stamp]=bar(stamp,direction).json()
            stamp+=60
        raw=source.poll(state)
        result,record=record_signal_input(state,raw)
        append_perp_market(case.vault,grant['id'],record,require_permission=lambda:None,clock_ns=lambda:clock['now'])
        state=result.state
        records.append(record)
        return state
    def emit():
        previous=state.evaluated_bars
        while state.evaluated_bars==previous:
            record_quote()
        return state
    def send(saved):
        signal=saved['latest_signal']
        _,parent,_=_signal_selection(saved['source'],saved['id'],signal)
        args=arguments(case)
        args['confirmation']='ENVIAR_ENTRADA_PERPETUA_GRVT_MAINNET'
        return submit_perp_entry_once(case.vault,terms,parent_client_order_id=parent,
            bot_selection=PerpBotSelectionRequest(saved['id'],signal['sha256']),**args)
    return SimpleNamespace(case=case,lab=lab,paper=paper,archive=archive,origin=origin,seed=seed,terms=terms,
        policy=policy,grant=grant,clock=clock,source=source,adapter=adapter,emit=emit,record_quote=record_quote,send=send,records=records)


def settle_owned_native(owned,parent,*,funding='0.125',loss=False,bad_fill=False,unlinked_funding=False,numeric_group=False):
    """Actual finance implementation over owned native fills/history only."""
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    from app.live_execution.grvt_mainnet_bot_entry import settle_bot_selection
    case=owned.case
    start=case.clock[0]
    case.clock[0]+=NS
    finish=case.clock[0]
    entry=case.settings['orders'][parent]
    stop=case.settings['orders'][str(int(parent)^1)]
    target=case.settings['orders'][str(int(parent)^2)]
    quantity=entry['legs'][0]['size']
    filled,cancelled=(stop,target) if loss else (target,stop)
    cancelled['state'].update(status='CANCELLED',update_time=str(finish))
    filled['state'].update(status='FILLED',traded_size=[quantity],update_time=str(finish))
    long=owned.terms.definition['direction']=='LONG'
    price=Decimal(filled['legs'][0]['limit_price'])
    if bad_fill:
        price+=Decimal('-0.01') if long else Decimal('0.01')
    pnl=(price-100)*Decimal(quantity)*(1 if long else -1)
    def fill(order,identifier,stamp,price,fee,realized):
        return {'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':identifier,
            'order_id':order['order_id'],'client_order_id':order['metadata']['client_order_id'],
            'event_time':str(stamp),'is_buyer':order['legs'][0]['is_buying_asset'],'is_taker':True,
            'size':quantity,'price':str(price),'fee':fee,'fee_currency':'USDT','realized_pnl':str(realized),
            'builder_fee':'0','is_liquidation':False}
    exit_id='20000-2' if numeric_group else 'owned-perp-bot-exit'
    entry_id='10000-1' if numeric_group else 'owned-perp-bot-entry'
    case.settings['histories']['fill_history']=[fill(filled,exit_id,finish,price,'0.03',pnl),
        fill(entry,entry_id,start,100,'0.02',0)]
    case.settings['histories']['funding_payment_history']=[{'sub_account_id':'123456',
        'tx_id':'owned-unlinked-funding' if unlinked_funding else '20000' if numeric_group else exit_id,
        'event_time':str(finish),'instrument':'BTC_USDT_Perp','currency':'USDT','amount':funding}]
    net=pnl-Decimal('.05')-Decimal(funding)
    case.settings['account_balance']=case.settings['account_equity']=str(100+net)
    case.settings.update(positions=[],initial_margin='0',maintenance_margin='0',unrealized_pnl='0')
    for _ in range(2):
        audit_mainnet_bracket(case.vault,parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.clock[0]+=6*NS
    receipt=reconcile_mainnet_bracket(case.vault,parent,now_ns=case.clock[0])
    saved=settle_bot_selection(case.vault,owned.grant['id'],now_ns=case.clock[0])
    owned.clock['now']=case.clock[0]
    return receipt,saved,net
