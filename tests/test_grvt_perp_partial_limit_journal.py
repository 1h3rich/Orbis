"""Versioned LIMIT entry, partial IOC recovery and final closure on owned data."""
from copy import deepcopy
from decimal import Decimal as D
import json
from types import SimpleNamespace

import httpx
import pytest

from app.lab.perp_dataset import TradeBar
from app.live_execution import grvt_perp_limit_supervisor as supervisor
from app.live_execution.grvt_perp_limit_exit import audit_limit_exit
from app.live_execution.grvt_perp_limit_exit_journal import partial_limit_template
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_journal import encode, stop_mainnet_subaccount
from app.live_execution.grvt_mainnet_preflight import canonical
from app.live_execution.grvt_mainnet_transport import BULK_URL, TICKER_URL
from app.live_execution.grvt_perp_limit_journal import _plan, read_limit_attempt, verified_limit_attempt
from app.live_execution.grvt_perp_limit_executions import limit_executions_view
from app.live_execution.grvt_perp_limit_receipt import LimitObservationPending
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import replay_signal_records
from app.portfolio.models import GrvtMainnetLimitAttempt, GrvtMainnetLimitEvent, GrvtMainnetLimitReduction
from tests.perp_observed_source_fixture import OwnedPublicAdapter
from tests.test_grvt_perp_limit_entry import CID, NS, fill
from tests.test_grvt_perp_limit_execution import cold, setup
from tests.test_grvt_perp_limit_exit import complete_entry, position
from tests.test_grvt_testnet_signer import KEY, SIGNER


def partial_case(tmp_path,monkeypatch,side):
    case,entry_posts,session,send = setup(tmp_path,monkeypatch,direction=side,partial=True)
    entry = complete_entry(case,session,send,side,partial=True)
    assert entry['terminal'] and entry['filled_size']=='0.5'
    saved = read_limit_attempt(case.vault,CID)
    assert saved['claim']['schema']=='orbis.grvt.mainnet.perp.limit.claim.v2'
    assert saved['claim']['preflight']['plan']['source']['terms']==send['terms'].payload()
    feed = replay_signal_records(send['seed'],send['records']).state
    fixture = SimpleNamespace(history=[TradeBar.model_validate(bar.json()) for bar in feed.history],
                              funding=[],quote=send['seed'].quote)
    adapter = OwnedPublicAdapter(fixture,{'now':case.clock[0]})
    adapter.clock = {'now':case.clock[0]}
    def clock():
        adapter.clock['now'] = case.clock[0]
        return case.clock[0]
    source = GrvtPerpSignalSource(adapter=adapter,clock_ns=clock)
    original_public = case.public

    def set_quote(price):
        bid,ask = (D(price),D(price)+D('.01')) if side=='LONG' else (D(price)-D('.01'),D(price))
        adapter.quote_override = {'bid':canonical(bid),'ask':canonical(ask),'mark_price':price}

    def public(url,body):
        response = original_public(url,body)
        if not url.endswith('/ticker'):
            return response
        value = response.json()
        value['result'].update(best_bid_price=adapter.quote_override['bid'],best_ask_price=adapter.quote_override['ask'])
        return httpx.Response(200,json=value,request=httpx.Request('POST',url))

    original_factory = session['client_factory']
    posts = []
    sign = 1 if side=='LONG' else -1
    prices = ('103','103','106','96') if side=='LONG' else ('97','97','94','92')
    quantities = ('0.125','0.105','0.175','0.2')
    entry_price = D(entry['entry_notional'])/D(entry['filled_size'])

    def factory():
        client = original_factory()
        original = client._post

        def post(url,body,headers):
            if url!=BULK_URL or not body['orders'] or not body['orders'][0]['reduce_only']:
                return original(url,body,headers)
            index = len(posts)
            wire, = body['orders']
            cid = wire['metadata']['client_order_id']
            assert wire['legs'][0]['size']==quantities[index]
            assert wire['legs'][0]['is_buying_asset'] is (side=='SHORT')
            assert wire['time_in_force']=='IMMEDIATE_OR_CANCEL'
            probe = cold(case.vault.path)
            try:
                event = read_limit_attempt(probe,CID)['events']['reductions'][-1]
                claim = event['claim']
                assert claim['client_order_id']==cid and claim['wire_digest']==encode(wire)[1]
                assert claim['decision']['target_index']==(1,1,2,None)[index]
                assert claim['decision']['quantity']==quantities[index]
                assert event['ack'] is None
            finally:
                probe.lock()
            posts.append(cid)
            at = str(case.clock[0])
            actual = D('0.02') if index==0 else D(quantities[index])
            order_id = f'0x89{index}'
            venue = {**deepcopy(wire),'order_id':order_id,'state':{
                'status':'CANCELLED' if index==0 else 'FILLED','book_size':['0'],'traded_size':[canonical(actual)],
                'update_time':at,'reject_reason':'IOC_CANCEL' if index==0 else 'UNSPECIFIED'}}
            venue['metadata']['create_time'] = at
            case.settings['orders'][cid] = venue
            pnl = sign*actual*(D(prices[index])-entry_price)
            case.settings['histories']['fill_history'].append(fill(trade_id=f'partial-{index}',order_id=order_id,
                client_order_id=cid,event_time=at,is_buyer=side=='SHORT',is_taker=True,
                size=canonical(actual),price=prices[index],fee='0',realized_pnl=canonical(pnl)))
            remaining = D(case.settings['positions'][0]['size'])-sign*actual
            balance = canonical(D(case.settings['account_balance'])+pnl)
            case.settings.update(account_balance=balance,account_equity=balance,
                                 positions=[position(canonical(remaining))] if remaining else [])
            if index==0:
                raise httpx.ReadTimeout('Owned IOC response lost')
            return httpx.Response(200,json={'result':{'orders':[venue],'cancel_acks':[]}},
                                  request=httpx.Request('POST',url))
        client._post = post
        return client

    ids = iter(range(200,230))
    monkeypatch.setattr(supervisor,'secrets',SimpleNamespace(randbits=lambda _bits:next(ids)))
    args = {**session,'client_factory':factory,'source':source,'public_post':public,
            'confirmation':'SUPERVISAR_ORDEN_LIMIT_PARCIALES_GRVT_MAINNET','max_reductions':6,
            'deadline_ns':case.clock[0]+120*NS,'private_key':KEY,'expected_signer':SIGNER}
    set_quote(prices[0])
    return case,entry_posts,posts,args,set_quote,prices


def assert_rehashed_target_rejected(vault,cid):
    with vault.session() as db:
        attempt = db.get(GrvtMainnetLimitAttempt,CID)
        reduction = db.get(GrvtMainnetLimitReduction,cid)
        payload = json.loads(reduction.payload_json)
        payload['decision']['target_index'] = 2
        reduction.payload_json,reduction.payload_digest = encode(payload)
        row = db.get(GrvtMainnetLimitEvent,(CID,attempt.event_count))
        event = json.loads(row.payload_json)
        assert event['kind']=='EXIT_CLAIM'
        event['detail']['claim_digest'] = reduction.payload_digest
        row.payload_json,row.payload_digest = encode(event)
        attempt.event_head = row.payload_digest
        db.flush()
        with pytest.raises(GrvtMainnetError) as error:
            verified_limit_attempt(db,attempt)
        assert 'objetivo y su cantidad' in str(error.value.__cause__)
        db.rollback()


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_partial_limit_ioc_survives_missing_reply_and_restarts_with_only_the_remainder(tmp_path,monkeypatch,side):
    case,entry_posts,posts,args,set_quote,prices = partial_case(tmp_path,monkeypatch,side)
    with pytest.raises(GrvtMainnetError,match='no corresponde'):
        supervisor.LimitSupervisor(case.vault,CID,**{**args,'confirmation':'SUPERVISAR_ORDEN_LIMIT_GRVT_MAINNET',
                                                   'max_reductions':4})
    controller = supervisor.LimitSupervisor(case.vault,CID,**args)
    case.clock[0] += NS
    assert controller.step()['status']=='VERIFYING_EXIT' and not posts
    case.clock[0] += 5*NS
    assert controller.step()['status']=='VERIFYING_EXIT' and len(posts)==1
    first = posts[0]
    assert_rehashed_target_rejected(case.vault,first)
    original = read_limit_attempt(case.vault,CID)
    assert original['events']['reductions'][0]['ack'] is None
    controller.stop()
    with pytest.raises(GrvtMainnetError,match='revocada'):
        controller.step()
    case.vault.lock()
    case.vault = cold(case.vault.path)
    try:
        assert read_limit_attempt(case.vault,CID)==original and not original['authorization']
        controller = supervisor.LimitSupervisor(case.vault,CID,**args)
        case.settings['order_lookup_errors'][first] = (404,1004)
        case.settings['order_history'] = lambda _body:{'result':[],'next':''}
        case.clock[0] += NS
        with pytest.raises(LimitObservationPending):
            controller.step()
        assert len(posts)==1
        case.settings['order_lookup_errors'].clear()
        case.clock[0] += NS
        assert controller.step()['status']=='VERIFYING_EXIT' and len(posts)==1
        case.clock[0] += 5*NS
        assert controller.step()['status']=='VERIFYING_EXIT' and len(posts)==2

        for price,expected in ((prices[2],3),(prices[3],4)):
            set_quote(price)
            case.clock[0] += NS
            assert controller.step()['status']=='VERIFYING_EXIT' and len(posts)==expected-1
            case.clock[0] += 5*NS
            assert controller.step()['status']=='VERIFYING_EXIT' and len(posts)==expected
        case.clock[0] += NS
        assert controller.step()['status']=='VERIFYING_EXIT'
        case.clock[0] += 5*NS
        final = controller.step()
        assert final['status']=='COMPLETE'
        assert final['decision']['close_reason']==('STOP_OR_TRAILING' if side=='LONG' else 'TAKE_PROFIT')
        saved = read_limit_attempt(case.vault,CID)
        assert saved['events']['position_settled'] and not saved['slot_reserved']
        result = saved['events']['exit_audits'][-1]['result']
        assert [order['filled_size'] for order in result['orders']]==['0.02','0.105','0.175','0.2']
        assert result['remaining_size']=='0' and result['net_change_usdt']==('0.67' if side=='LONG' else '3.07')
        assert result['settle_balance']==('100.67' if side=='LONG' else '103.07') and len(entry_posts)==2
        chart = limit_executions_view(saved)
        assert [row['target_index'] for row in chart['executions'] if row['role']=='REDUCTION']==[1,1,2,None]
        assert chart['financially_reconciled'] and chart['net_realized_pnl_usdt']==result['net_change_usdt']
    finally:
        case.vault.lock()


def test_partial_supervisor_stops_before_the_next_reduction_on_revoked_control(tmp_path,monkeypatch):
    case,_,posts,args,_,_ = partial_case(tmp_path,monkeypatch,'LONG')
    controller = supervisor.LimitSupervisor(case.vault,CID,**args)
    try:
        case.clock[0] += NS
        assert controller.step()['status']=='VERIFYING_EXIT' and not posts
        stop_mainnet_subaccount(case.vault,'123456')
        case.clock[0] += 5*NS
        with pytest.raises(GrvtMainnetError,match='revocada'):
            controller.step()
        saved = read_limit_attempt(case.vault,CID)
        assert saved['slot_reserved'] and not saved['events']['reductions'] and not posts
        assert controller._key is None
    finally:
        case.vault.lock()


def test_newer_quote_can_latch_full_target_but_same_time_disagreement_is_rejected(tmp_path,monkeypatch):
    case,_,posts,args,_,_ = partial_case(tmp_path,monkeypatch,'LONG')
    controller = supervisor.LimitSupervisor(case.vault,CID,**args)
    try:
        case.clock[0] += NS
        assert controller.step()['status']=='VERIFYING_EXIT'
        case.clock[0] += 5*NS
        archived = audit_limit_exit(case.vault,CID,**{key:args[key] for key in
            ('client_factory','require_permission','clock_ns')})
        claim,state = archived['claim'],archived['events']
        plan = _plan(claim,claim['claimed_at_ns'])
        assert state['supervision']['close_reason'] is None
        now = case.clock[0]
        quote = {'source':TICKER_URL,'instrument':'BTC_USDT_Perp','event_time_ns':now,'observed_at_ns':now,
            'bid':'110','ask':'110.01','bid_size':'5','ask_size':'5'}
        options = dict(head=state['supervision_head'],cid=str(int(CID)+500),
                       nonce=17,issued=now,expiry=now+10*NS)
        latest = state['exit_audits'][-1]
        template,decision = partial_limit_template(plan,state,latest,quote,**options)
        assert decision['action']=='PARTIAL' and decision['target_index']==1
        assert decision['take_profit_latched'] and template['legs'][0]['size']=='0.125' and not posts
        disputed = {**quote,'event_time_ns':int(state['supervision']['quote']['event_time_ns'])}
        with pytest.raises(ValueError,match='divergente'):
            partial_limit_template(plan,state,latest,disputed,**options)
    finally:
        controller.stop()
        case.vault.lock()
