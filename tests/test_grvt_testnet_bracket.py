"""Entrada nativa FOK + SL/TP vinculados; sólo HTTP y claves de prueba."""

from dataclasses import replace
from decimal import Decimal

from tests.test_grvt_testnet_signer import NOW,draft,metadata,limits
from tests.test_grvt_testnet_risk import state,rules
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
from tests.test_grvt_testnet_signer import KEY,SIGNER
from eth_account import Account
from eth_account.messages import encode_typed_data
import pytest
import httpx
from copy import deepcopy

from app.live_execution.grvt_testnet_order import AUTH_URL,BULK_ORDERS_URL,GrvtTestnetOrderClient,GrvtTestnetOrderError


def plan_for(side='BUY'):
    from app.live_execution.grvt_testnet_bracket import compile_testnet_bracket
    entry = replace(draft(),side=side,builder='0x'+'0'*40)
    quote = GrvtTestnetQuote(entry.instrument,NOW,NOW,Decimal('49999'),Decimal('50001'),
                            Decimal('10'),Decimal('10'),last_price=Decimal('50000'))
    return compile_testnet_bracket(entry,state(),rules(),metadata(),limits(),limits(),quote,
        stop_loss=Decimal('49000' if side == 'BUY' else '51000'),
        take_profit=Decimal('53000' if side == 'BUY' else '47000'),max_spread_bps=25,
        max_slippage_bps=25,exit_nonces=(43,44),exit_expiration_ns=NOW+3_600_000_000_000,now_ns=NOW)


def test_planning_a_linked_entry_keeps_real_flat_state_and_exact_three_orders():
    from app.live_execution.grvt_testnet_bracket import compile_testnet_bracket,bracket_client_ids
    actual = state()
    entry = replace(draft(),builder='0x'+'0'*40)
    quote = GrvtTestnetQuote(entry.instrument,NOW,NOW,Decimal('49999'),Decimal('50001'),
                            Decimal('10'),Decimal('10'),last_price=Decimal('50000'))
    plan = compile_testnet_bracket(entry,actual,rules(),metadata(),limits(),limits(),quote,
        stop_loss=Decimal('49000'),take_profit=Decimal('53000'),max_spread_bps=25,
        max_slippage_bps=25,exit_nonces=(43,44),exit_expiration_ns=NOW+3_600_000_000_000,now_ns=NOW)
    assert actual.positions == ()  # No se inventa una posición observada.
    assert plan.entry == entry
    assert [plan.stop_loss.client_order_id,plan.take_profit.client_order_id] == list(bracket_client_ids(entry.client_order_id)[1:])
    assert plan.stop_loss.side == plan.take_profit.side == 'SELL'
    assert plan.stop_loss.size == plan.take_profit.size == entry.size
    assert plan.stop_loss.limit_price == Decimal('48877.5')
    assert plan.take_profit.limit_price == Decimal('52867.5')


def test_linked_parent_is_signed_as_native_fok_not_ioc_and_children_are_gtt():
    from app.live_execution.grvt_testnet_bracket import sign_testnet_bracket,verify_testnet_bracket
    from app.live_execution.grvt_testnet_signer import _ORDER_WITHOUT_BUILDER_TYPES,verify_testnet_limit_signature,GrvtTestnetSignError
    plan = plan_for()
    orders = sign_testnet_bracket(plan,limits(),limits(),metadata(),
        dedicated_private_key=KEY,expected_signer=SIGNER,now_ns=NOW)
    assert [order['time_in_force'] for order in orders] == ['FILL_OR_KILL','GOOD_TILL_TIME','GOOD_TILL_TIME']
    assert [order['reduce_only'] for order in orders] == [False,True,True]
    assert verify_testnet_bracket(orders,limits(),limits(),metadata(),
        expected_triggers=(plan.stop_trigger,plan.target_trigger),now_ns=NOW) == SIGNER
    # Contraste independiente del enum oficial FOK=4 y del tipo Order sin builder.
    signature = orders[0]['signature']
    message = {'subAccountID':123456,'isMarket':False,'timeInForce':4,'postOnly':False,'reduceOnly':False,
        'legs':[{'assetID':123456789,'contractSize':10,'limitPrice':50_000_000_000_000,'isBuyingContract':True}],
        'nonce':42,'expiration':plan.entry.expiration_ns}
    signable = encode_typed_data({'name':'GRVT Exchange','version':'0','chainId':326},_ORDER_WITHOUT_BUILDER_TYPES,message)
    assert Account.recover_message(signable,vrs=(signature['v'],int(signature['r'],16),int(signature['s'],16))) == SIGNER
    with pytest.raises(GrvtTestnetSignError):
        verify_testnet_limit_signature(orders[0],limits(),metadata(),now_ns=NOW)
    assert KEY.hex() not in str(orders)


def signed_group():
    from app.live_execution.grvt_testnet_bracket import sign_testnet_bracket
    return sign_testnet_bracket(plan_for(),limits(),limits(),metadata(),
        dedicated_private_key=KEY,expected_signer=SIGNER,now_ns=NOW)


def test_native_linked_group_uses_one_bulk_and_shared_creation_allowance():
    orders = signed_group()
    calls = []
    def post(url,body,headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(200,json={'status':'success','sub_account_id':'123456'},
                headers={'X-Grvt-Account-Id':'0x'+'a'*40,'Set-Cookie':'gravity=fake-testnet-cookie; Path=/'},
                request=httpx.Request('POST',url))
        assert url == BULK_ORDERS_URL
        assert body == {'sub_account_id':'123456','orders':orders,'order_i_ds':[],'client_order_i_ds':[]}
        return httpx.Response(200,json={'result':{'orders':[
            {**deepcopy(order),'order_id':'0xbracket'+str(i)} for i,order in enumerate(orders)],'cancel_acks':[]}})
    client = GrvtTestnetOrderClient('fake-testnet-key',http_post=post,clock_ns=lambda:NOW)
    acks = client.submit_signed_bracket(orders,limits(),limits())
    assert len(acks) == 3 and all(ack.execution_unverified for ack in acks)
    assert calls == [AUTH_URL,BULK_ORDERS_URL]
    with pytest.raises(GrvtTestnetOrderError,match='consumida'):
        client.submit_signed_bracket(orders,limits(),limits())
    with pytest.raises(GrvtTestnetOrderError,match='consumida'):
        client.submit_signed_limit(orders[0],limits())
    assert calls == [AUTH_URL,BULK_ORDERS_URL]


def test_concurrent_ioc_and_linked_entry_share_one_atomic_transport_allowance():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.live_execution.grvt_testnet_order import ORDER_URL
    from app.live_execution.grvt_testnet_signer import sign_testnet_limit
    barrier = Barrier(2)
    calls = []
    group = signed_group()
    ioc = sign_testnet_limit(plan_for().entry,limits(),metadata(),
        dedicated_private_key=KEY,expected_signer=SIGNER,now_ns=NOW)
    def post(url,body,headers):
        if url == AUTH_URL:
            return httpx.Response(200,json={'status':'success','sub_account_id':'123456'},
                headers={'X-Grvt-Account-Id':'0x'+'a'*40,'Set-Cookie':'gravity=fake-testnet-cookie; Path=/'},
                request=httpx.Request('POST',url))
        calls.append(url)
        if url == ORDER_URL: return httpx.Response(200,json={'result':{**body['order'],'order_id':'0xioc'}})
        return httpx.Response(200,json={'result':{'orders':[
            {**deepcopy(order),'order_id':'0xbracket'+str(i)} for i,order in enumerate(body['orders'])],'cancel_acks':[]}})
    def clock():
        barrier.wait(timeout=5)
        return NOW
    client = GrvtTestnetOrderClient('fake-testnet-key',http_post=post,clock_ns=clock)
    def submit(linked):
        try:
            if linked: client.submit_signed_bracket(group,limits(),limits())
            else: client.submit_signed_limit(ioc,limits())
            return 'SENT'
        except GrvtTestnetOrderError: return 'CONSUMED'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit,(False,True)))
    assert sorted(results) == ['CONSUMED','SENT'] and len(calls) == 1


@pytest.mark.parametrize('side',['BUY','SELL'])
def test_native_observation_reports_partial_fok_as_review_not_a_protected_entry(side):
    from app.live_execution.grvt_testnet_bracket import sign_testnet_bracket,observe_testnet_bracket
    from app.live_execution.grvt_testnet_order import LOOKUP_URL
    plan = plan_for(side)
    orders = sign_testnet_bracket(plan,limits(),limits(),metadata(),
        dedicated_private_key=KEY,expected_signer=SIGNER,now_ns=NOW)
    calls = []
    def post(url,body,headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(200,json={'status':'success','sub_account_id':'123456'},
                headers={'X-Grvt-Account-Id':'0x'+'a'*40,'Set-Cookie':'gravity=fake-testnet-cookie; Path=/'},
                request=httpx.Request('POST',url))
        assert url == LOOKUP_URL
        index = next(i for i,value in enumerate(orders) if value['metadata']['client_order_id'] == body['client_order_id'])
        value = deepcopy(orders[index])
        value.update(order_id='0xlinked'+str(index),state={'status':'CANCELLED' if index == 0 else 'PENDING',
            'traded_size':['0.003' if index == 0 else '0'],'update_time':str(NOW+1_000_000_000)})
        value['metadata']['create_time'] = str(NOW+1)
        return httpx.Response(200,json={'result':value})
    client = GrvtTestnetOrderClient('fake-testnet-key',http_post=post,clock_ns=lambda:NOW+2_000_000_000)
    observed = observe_testnet_bracket(client,orders,limits(),limits(),metadata(),
        created_at_ns=NOW,clock_ns=lambda:NOW+2_000_000_000)
    assert observed['scope'] == 'ORDER_STATE_ONLY'
    assert observed['fok_partial_violation'] is True and observed['needs_review'] is True
    assert observed['orders'][0]['traded_size'] == '0.003'
    assert observed['entry_fully_filled'] is False and observed['protection_guaranteed'] is False
    assert observed['residual_size_from_orders'] == '0.003'
    assert BULK_ORDERS_URL not in calls and len(calls) == 4
    assert SIGNER not in str(observed) and KEY.hex() not in str(observed)


def test_observed_parent_expiration_must_remain_a_string_not_a_rounded_number():
    from app.live_execution.grvt_testnet_bracket import observe_testnet_bracket,GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_order import LOOKUP_URL
    orders = signed_group()
    def post(url,body,headers):
        if url == AUTH_URL:
            return httpx.Response(200,json={'status':'success','sub_account_id':'123456'},
                headers={'X-Grvt-Account-Id':'0x'+'a'*40,'Set-Cookie':'gravity=fake-testnet-cookie; Path=/'},
                request=httpx.Request('POST',url))
        assert url == LOOKUP_URL
        index = next(i for i,value in enumerate(orders) if value['metadata']['client_order_id'] == body['client_order_id'])
        value = deepcopy(orders[index]); value['order_id'] = '0xtyped'+str(index)
        if index == 0: value['signature']['expiration'] = float(value['signature']['expiration'])
        value['metadata']['create_time'] = str(NOW+1)
        value['state'] = {'status':'OPEN','traded_size':['0'],'update_time':str(NOW+1_000_000_000)}
        return httpx.Response(200,json={'result':value})
    client = GrvtTestnetOrderClient('fake-testnet-key',http_post=post,clock_ns=lambda:NOW+2_000_000_000)
    with pytest.raises(GrvtTestnetBracketError,match='no verificable'):
        observe_testnet_bracket(client,orders,limits(),limits(),metadata(),created_at_ns=NOW,
            clock_ns=lambda:NOW+2_000_000_000)


@pytest.mark.parametrize('fault',['timeout','missing','trigger','parent_tif','foreign_id','same_external_id','expiry_number'])
def test_uncertain_or_altered_bulk_reply_consumes_allowance_without_fallback(fault):
    orders = signed_group(); sends = []
    def post(url,body,headers):
        if url == AUTH_URL:
            return httpx.Response(200,json={'status':'success','sub_account_id':'123456'},
                headers={'X-Grvt-Account-Id':'0x'+'a'*40,'Set-Cookie':'gravity=fake-testnet-cookie; Path=/'},
                request=httpx.Request('POST',url))
        sends.append(url); assert url == BULK_ORDERS_URL
        if fault == 'timeout': raise httpx.ReadTimeout('fake unknown result')
        rows = [{**deepcopy(order),'order_id':'0xbracket'+str(i)} for i,order in enumerate(body['orders'])]
        if fault == 'missing': rows.pop()
        if fault == 'trigger': rows[1]['metadata']['trigger']['tpsl']['trigger_price'] = '48999.9'
        if fault == 'parent_tif': rows[0]['time_in_force'] = 'IMMEDIATE_OR_CANCEL'
        if fault == 'foreign_id': rows[2]['metadata']['client_order_id'] = str(2**63+123456)
        if fault == 'same_external_id': rows[2]['order_id'] = rows[0]['order_id']
        if fault == 'expiry_number': rows[0]['signature']['expiration'] = int(rows[0]['signature']['expiration'])
        return httpx.Response(200,json={'result':{'orders':rows,'cancel_acks':[]}})
    client = GrvtTestnetOrderClient('fake-testnet-key',http_post=post,clock_ns=lambda:NOW)
    with pytest.raises(GrvtTestnetOrderError,match='desconocido'):
        client.submit_signed_bracket(orders,limits(),limits())
    with pytest.raises(GrvtTestnetOrderError,match='consumida'):
        client.submit_signed_bracket(orders,limits(),limits())
    assert sends == [BULK_ORDERS_URL]


@pytest.mark.parametrize('fault',['position','margin','balance','stale','missing_last','trigger_tick',
    'size_precision','entry_tick','entry_ttl','exit_ttl','same_nonce','bool_nonce','exit_cap','inverted'])
def test_linked_entry_rejects_unverifiable_risk_or_terms_before_signing(fault):
    from app.live_execution.grvt_testnet_bracket import compile_testnet_bracket,GrvtTestnetBracketError
    entry = plan_for().entry; account = state(); exit_cap = limits()
    quote = GrvtTestnetQuote(entry.instrument,NOW,NOW,Decimal('49999'),Decimal('50001'),
                            Decimal('10'),Decimal('10'),last_price=Decimal('50000'))
    params = dict(stop_loss=Decimal('49000'),take_profit=Decimal('53000'),max_spread_bps=25,
        max_slippage_bps=25,exit_nonces=(43,44),exit_expiration_ns=NOW+3_600_000_000_000,now_ns=NOW)
    if fault == 'position': account = replace(account,positions=((entry.instrument,Decimal('0.001')),))
    if fault == 'margin': account = replace(account,initial_margin=Decimal('1'))
    if fault == 'balance': account = replace(account,settle_balance=Decimal('5'),total_equity=Decimal('5'))
    if fault == 'stale': account = replace(account,event_time_ns=NOW-6_000_000_000)
    if fault == 'missing_last': quote = replace(quote,last_price=None)
    if fault == 'trigger_tick': params['stop_loss'] = Decimal('49000.01')
    if fault == 'size_precision': entry = replace(entry,size=Decimal('0.0101'))
    if fault == 'entry_tick': entry = replace(entry,limit_price=Decimal('50000.01'))
    if fault == 'entry_ttl': entry = replace(entry,expiration_ns=NOW+301_000_000_000)
    if fault == 'exit_ttl': params['exit_expiration_ns'] = entry.expiration_ns
    if fault == 'same_nonce': params['exit_nonces'] = (entry.nonce,44)
    if fault == 'bool_nonce': params['exit_nonces'] = (True,44)
    if fault == 'exit_cap': exit_cap = replace(exit_cap,max_order_notional=Decimal('500'))
    if fault == 'inverted': params['stop_loss'],params['take_profit'] = params['take_profit'],params['stop_loss']
    with pytest.raises(GrvtTestnetBracketError):
        compile_testnet_bracket(entry,account,rules(),metadata(),limits(),exit_cap,quote,**params)


def test_signing_cannot_bypass_minimum_protection_lifetime_with_a_handmade_draft():
    from app.live_execution.grvt_testnet_bracket import sign_testnet_bracket,GrvtTestnetBracketError
    plan = plan_for()
    changed = replace(plan,entry=replace(plan.entry,expiration_ns=NOW+10_000_000_000),
        stop_loss=replace(plan.stop_loss,expiration_ns=NOW+30_000_000_000),
        take_profit=replace(plan.take_profit,expiration_ns=NOW+30_000_000_000))
    with pytest.raises(GrvtTestnetBracketError):
        sign_testnet_bracket(changed,limits(),limits(),metadata(),dedicated_private_key=KEY,
            expected_signer=SIGNER,now_ns=NOW)


def test_order_observation_refuses_a_clock_that_recedes_between_members():
    from app.live_execution.grvt_testnet_bracket import observe_testnet_bracket,GrvtTestnetBracketError
    orders = signed_group()
    def post(url,body,headers):
        if url == AUTH_URL:
            return httpx.Response(200,json={'status':'success','sub_account_id':'123456'},
                headers={'X-Grvt-Account-Id':'0x'+'a'*40,'Set-Cookie':'gravity=fake-testnet-cookie; Path=/'},
                request=httpx.Request('POST',url))
        index = next(i for i,value in enumerate(orders) if value['metadata']['client_order_id'] == body['client_order_id'])
        value = deepcopy(orders[index]); value['order_id'] = '0xclock'+str(index)
        value['metadata']['create_time'] = str(NOW+1)
        value['state'] = {'status':'OPEN','traded_size':['0'],'update_time':str(NOW+1_000_000_000)}
        return httpx.Response(200,json={'result':value})
    readings = iter([NOW+5_000_000_000,NOW+7_000_000_000,NOW+6_000_000_000,NOW+8_000_000_000,NOW+9_000_000_000])
    client = GrvtTestnetOrderClient('fake-testnet-key',http_post=post,clock_ns=lambda:NOW+10_000_000_000)
    with pytest.raises(GrvtTestnetBracketError):
        observe_testnet_bracket(client,orders,limits(),limits(),metadata(),created_at_ns=NOW,clock_ns=lambda:next(readings))
