"""Owned LIMIT orders, signed reductions and replay through a controlled HTTP exchange."""
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from app.live_execution import grvt_perp_limit_execution as execution
from app.live_execution import grvt_perp_limit_exit as exit_service
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_journal import encode, require_new_mainnet_bracket
from app.live_execution.grvt_mainnet_transport import BULK_URL, TICKER_URL
from app.live_execution.grvt_perp_limit_exit_evidence import limit_reduction_template
from app.live_execution.grvt_perp_limit_journal import read_limit_attempt
from app.live_execution.grvt_perp_limit_executions import limit_executions_view
from app.live_execution.grvt_perp_limit_plan import build_limit_plan
from tests.test_grvt_perp_limit_entry import CID, NS, fill, source
from tests.test_grvt_perp_limit_execution import SUB, VENUE_ORDER_ID, cold, enable_order_history, setup
from tests.test_grvt_perp_plan import NOW
from tests.test_grvt_testnet_signer import KEY, SIGNER


INSTRUMENT = 'BTC_USDT_Perp'
FIRST_EXIT = str(int(CID) + 100)
SECOND_EXIT = str(int(CID) + 200)


def position(size):
    return {'sub_account_id':SUB,'instrument':INSTRUMENT,'size':size}


def complete_entry(case, session, send, direction, *, partial=False):
    submit = execution.submit_partial_limit_entry_once if partial else execution.submit_limit_entry_once
    submit(case.vault,**send)
    case.clock[0] += NS
    order = case.settings['orders'][CID]
    order['state'].update(traded_size=['0.3'],update_time=str(case.clock[0]),
                          book_size=[str(Decimal(order['legs'][0]['size'])-Decimal('0.3'))])
    price = '99.9' if direction == 'LONG' else '100.1'
    case.settings['histories']['fill_history'] = [
        fill(direction,size='0.3',price=price,order_id=VENUE_ORDER_ID,event_time=str(case.clock[0]))]
    case.clock[0] += NS
    execution.cancel_limit_attempt_once(case.vault,CID,**session,confirmation='CANCELAR_ENTRADA_LIMIT_GRVT_MAINNET')
    case.settings['histories']['funding_payment_history'] = [
        {'sub_account_id':SUB,'event_time':str(case.clock[0]),'tx_id':'during-cancel',
         'instrument':INSTRUMENT,'currency':'USDT','amount':'0.005'}]
    case.settings.update(account_balance='99.995',account_equity='99.995',
                         positions=[position('0.5' if direction == 'LONG' else '-0.5')])
    case.clock[0] += NS
    return execution.observe_limit_attempt(case.vault,CID,**session)['events']['receipt']


def controlled_exit(case, session, direction):
    previous_factory = session['client_factory']
    posts = []

    def factory():
        client = previous_factory()
        previous_post = client._post

        def post(url, body, headers):
            if url != BULK_URL or not body['orders'] or body['orders'][0]['reduce_only'] is not True:
                return previous_post(url,body,headers)
            assert not body['order_i_ds'] and not body['client_order_i_ds'] and body['sub_account_id'] == SUB
            wire, = body['orders']
            posts.append(wire)
            first = len(posts) == 1
            assert len(posts) <= 2 and wire['time_in_force'] == 'IMMEDIATE_OR_CANCEL'
            cid = wire['metadata']['client_order_id']
            assert cid == (FIRST_EXIT if first else SECOND_EXIT)
            assert wire['legs'][0]['is_buying_asset'] is (direction == 'SHORT')
            assert wire['legs'][0]['size'] == ('0.5' if first else '0.3')
            probe = cold(case.vault.path)
            try:
                events = read_limit_attempt(probe,CID)['events']
                reduction = events['reductions'][-1]
                assert reduction['claim']['client_order_id'] == cid and reduction['ack'] is None
                assert reduction['claim']['wire_digest'] == encode(wire)[1]
                assert not events['exit_audits']
            finally:
                probe.lock()
            at = str(case.clock[0])
            filled = '0.2' if first else '0.3'
            order_id = '0x885' if first else '0x886'
            raw = {**deepcopy(wire),'order_id':order_id,'state':{
                'status':'CANCELLED' if first else 'FILLED','book_size':['0'],'traded_size':[filled],
                'update_time':at,'reject_reason':'IOC_CANCEL' if first else 'UNSPECIFIED'}}
            raw['metadata']['create_time'] = at
            case.settings['orders'][cid] = raw
            case.settings['histories']['fill_history'].append(
                fill(trade_id=f'ioc-{len(posts)}',order_id=order_id,client_order_id=cid,event_time=at,
                     is_buyer=direction=='SHORT',is_taker=True,size=filled,price='100',fee='0.01',
                     realized_pnl='0.02' if first else '0.03'))
            balance = '100.005' if first else '100.025'
            remaining = '0.3' if direction == 'LONG' else '-0.3'
            case.settings.update(account_balance=balance,account_equity=balance,
                                 positions=[position(remaining)] if first else [])
            if first:
                raise httpx.ReadTimeout('Owned IOC response lost')
            return httpx.Response(200,json={'result':{'orders':[raw],'cancel_acks':[]}},
                                  request=httpx.Request('POST',url))

        client._post = post
        return client

    return {**session,'client_factory':factory}, posts


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_terminal_partial_entry_two_reductions_and_cold_recovery(tmp_path, monkeypatch, direction):
    case, calls, session, send = setup(tmp_path,monkeypatch,direction=direction)
    entry = complete_entry(case,session,send,direction)
    assert entry['terminal'] and entry['filled_size'] == '0.5'
    assert entry['entry_notional'] == ('49.95' if direction == 'LONG' else '50.05')
    session, posts = controlled_exit(case,session,direction)
    if direction == 'SHORT':
        enable_order_history(case,FIRST_EXIT,SECOND_EXIT)
    audit, submit, settle = (exit_service.audit_limit_exit,exit_service.submit_limit_reduction_once,
                             exit_service.settle_limit_exit)
    vault = case.vault

    def reduce(cid):
        return submit(vault,CID,**session,reduction_client_order_id=cid,deadline_ns=case.clock[0]+20*NS,
                      confirmation='REDUCIR_POSICION_LIMIT_GRVT_MAINNET',private_key=KEY,
                      expected_signer=SIGNER,public_post=case.public)

    sign = '' if direction == 'LONG' else '-'
    case.settings['positions'] = [position(f'{sign}0.6')]
    with pytest.raises(GrvtMainnetError,match='requieren revisión'):
        audit(vault,CID,**session)
    assert read_limit_attempt(vault,CID)['events']['exit_audits'] == []
    case.settings['positions'] = [position(f'{sign}0.5')]
    first = audit(vault,CID,**session)['events']['exit_audits'][-1]['result']
    assert first['remaining_size'] == '0.5' and first['funding_paid_usdt'] == '0.005'
    with pytest.raises(GrvtMainnetError,match='dos auditorías'):
        reduce(FIRST_EXIT)
    assert not posts
    case.clock[0] += 5*NS
    audit(vault,CID,**session)
    with pytest.raises(GrvtMainnetError,match='sin confirmar'):
        reduce(FIRST_EXIT)
    assert len(posts) == 1

    vault.lock()
    vault = cold(vault.path)
    recovered = read_limit_attempt(vault,CID)
    assert recovered['slot_reserved'] and not recovered['authorization']
    assert recovered['events']['reductions'][0]['ack'] is None
    with pytest.raises(GrvtMainnetError,match='dos auditorías'):
        reduce(SECOND_EXIT)
    assert len(posts) == 1
    case.clock[0] += NS
    partial = audit(vault,CID,**session)['events']['exit_audits'][-1]['result']
    assert partial['orders'][0]['filled_size'] == '0.2' and partial['remaining_size'] == '0.3'
    assert partial['settle_balance'] == '100.005' and partial['net_change_usdt'] == '0.005'
    case.clock[0] += 5*NS
    audit(vault,CID,**session)
    with pytest.raises(GrvtMainnetError,match='identificador'):
        reduce(FIRST_EXIT)
    case.clock[0] += NS
    confirmed = reduce(SECOND_EXIT)
    assert confirmed['events']['reductions'][-1]['ack'] == '0x886'
    assert confirmed['slot_reserved']
    intermediate = limit_executions_view(confirmed)
    assert [row['role'] for row in intermediate['executions']] == ['ENTRY','ENTRY','REDUCTION']
    assert not intermediate['financially_reconciled']
    case.clock[0] += NS
    earlier_fill = case.settings['histories']['fill_history'][-2]
    earlier_fill['trade_id'] = 'rewritten-ioc-1'
    with pytest.raises(GrvtMainnetError,match='verificable'):
        audit(vault,CID,**session)
    assert not read_limit_attempt(vault,CID)['events']['exit_audits']
    earlier_fill['trade_id'] = 'ioc-1'
    audit(vault,CID,**session)
    case.clock[0] += 5*NS
    result = audit(vault,CID,**session)['events']['exit_audits'][-1]['result']
    assert result['remaining_size'] == '0' and result['positions'] == []
    assert result['exit_notional'] == '50' and result['realized_pnl_usdt'] == '0.05'
    assert result['fees_usdt'] == '0.02' and result['net_change_usdt'] == '0.025'
    assert result['settle_balance'] == '100.025' and result['execution_authority'] == 'NONE'
    assert result['policy_breaches'] == ['FEE_CEILING_EXCEEDED']
    gate = {name:session[name] for name in ('require_permission','clock_ns')}
    final = settle(vault,CID,**gate)
    assert not final['slot_reserved'] and final['events']['position_settled']
    assert settle(vault,CID,**gate) == final
    vault.backup(tmp_path/'limit-closed.db')
    vault.lock()
    vault = cold(vault.path)
    assert read_limit_attempt(vault,CID) == final
    chart = limit_executions_view(final)
    assert [row['side'] for row in chart['executions']] == (['BUY','BUY','SELL','SELL'] if direction == 'LONG'
                                                           else ['SELL','SELL','BUY','BUY'])
    with vault.session() as db:
        require_new_mainnet_bracket(db,SUB,str(int(CID)+300))
        for cid in (CID,FIRST_EXIT,SECOND_EXIT):
            with pytest.raises(GrvtMainnetError,match='reservado'):
                require_new_mainnet_bracket(db,SUB,cid)
    assert len(calls) == len(posts) == 2
    assert any(name == 'order_history' for name,_ in case.settings['history_requests']) is (direction == 'SHORT')
    vault.lock()


@pytest.mark.parametrize(('side','buying','expected_size'),[
    ('LONG',False,'20.002'),('SHORT',True,'19.948'),
])
def test_reduction_direction_and_lot_respect_notional_cap(side, buying, expected_size):
    plan = build_limit_plan(source(side))
    quote = {'source':TICKER_URL,'instrument':INSTRUMENT,'event_time_ns':NOW,'observed_at_ns':NOW,
             'bid':'99.99','ask':'100.01','bid_size':'50','ask_size':'50'}
    wire = limit_reduction_template(plan,Decimal('30'),quote,cid=FIRST_EXIT,nonce=1,
                                    issued_at_ns=NOW,expiration_ns=NOW+10*NS)
    leg, = wire['legs']
    assert wire['reduce_only'] and wire['time_in_force'] == 'IMMEDIATE_OR_CANCEL'
    assert leg['is_buying_asset'] is buying and leg['size'] == expected_size
    bound = max(Decimal(leg['limit_price']),Decimal(quote['ask' if buying else 'bid']))
    assert Decimal(leg['size'])*bound <= 2000 < (Decimal(leg['size'])+Decimal('0.001'))*bound
