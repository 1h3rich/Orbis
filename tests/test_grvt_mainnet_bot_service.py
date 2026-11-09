"""Coordinador MAINNET con PAPER/LAB/firma/diario reales y sólo HTTP controlado."""
import time
from decimal import Decimal
from copy import deepcopy
from types import SimpleNamespace
import httpx
import pytest
from tests.test_grvt_mainnet_bot_entry import bot_case
from tests.test_grvt_testnet_signer import KEY,SIGNER
from tests.test_grvt_mainnet_submit import PASSWORD


def wait_for(predicate,seconds=15):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=predicate()
        if result:return result
        time.sleep(.01)
    raise AssertionError('El bot MAINNET no alcanzó el estado esperado')


def service_case(directory,direction="LONG",*,research=False):
    from app.live_execution.grvt_mainnet_bot_service import GrvtMainnetBotService
    case,origin,source,emit=bot_case(directory,direction,research=research)
    settings={'mark':'100'}
    def clock():
        case.clock[0]=max(case.clock[0],time.time_ns());return case.clock[0]
    def public(url,body):
        clock()
        if url.endswith('/kline'):
            start=int(body['start_time'])//1_000_000_000;end=(int(body['end_time'])+1)//1_000_000_000
            rows=[{'instrument':'BTC_USDT_Perp','open_time':str(t*1_000_000_000),'close_time':str((t+3600)*1_000_000_000),
                'open':'100','high':'101','low':'99','close':'100','volume_b':'10'} for t in range(start,end,3600)]
            return httpx.Response(200,json={'result':list(reversed(rows)),'next':''},request=httpx.Request('POST',url))
        reply=case.public(url,body)
        if url.endswith('/ticker'):
            data=reply.json();data['result']['mark_price']=settings['mark']
            return httpx.Response(200,json=data,request=httpx.Request('POST',url))
        return reply
    recorded=set()
    def factory(key):
        client=case.factory();original=client._post
        def post(url,body,headers):
            lost=None
            try:reply=original(url,body,headers)
            except httpx.ReadTimeout as error:lost=error;reply=None
            if url.endswith('/bulk_orders') and len(body.get('orders',[]))==3 and body['orders'][0]['metadata']['client_order_id'] not in recorded:
                recorded.add(body['orders'][0]['metadata']['client_order_id'])
                entry=case.settings['orders'][case.settings['active_parent']];size=entry['legs'][0]['size'];stamp=entry['metadata']['create_time']
                case.settings['histories']['fill_history']=[{'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':'service-fill-'+stamp,
                    'order_id':entry['order_id'],'client_order_id':entry['metadata']['client_order_id'],'event_time':stamp,
                    'is_buyer':entry['legs'][0]['is_buying_asset'],'is_taker':True,'size':size,'price':'100','fee':'0.02','fee_currency':'USDT',
                    'realized_pnl':'0','builder_fee':'0','is_liquidation':False}]
                balance=Decimal(case.settings['account_balance'])-Decimal('.02')
                case.settings.update(account_balance=str(balance),account_equity=str(balance),initial_margin='1',maintenance_margin='0.5',
                    positions=[{'sub_account_id':'123456','instrument':'BTC_USDT_Perp','size':size if entry['legs'][0]['is_buying_asset'] else '-'+size}])
            if lost is not None:raise lost
            return reply
        client._post=post;return client
    def waiter(stop,seconds):
        if seconds>.2:case.clock[0]+=int(seconds*1_000_000_000)
        return stop.wait(.01)
    service=GrvtMainnetBotService(case.vault,client_factory=factory,public_post=public,clock_ns=clock,waiter=waiter,poll_seconds=.02)
    return case,origin,source,emit,settings,service


@pytest.mark.parametrize('direction,exit_mark',[('LONG','96.9'),('SHORT','103.1')])
def test_authorized_bot_waits_for_a_future_signal_and_closes_from_real_fills(tmp_path,direction,exit_mark):
    from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy,read_bot_grant
    case,origin,source,emit,settings,service=service_case(tmp_path,direction)
    policy=MainnetBotPolicy('123456',Decimal('50'),Decimal('100'),Decimal('50'),Decimal('10'))
    started=service.start(origin.paper,origin.lab,source['session_id'],policy,api_key='synthetic-mainnet-api-key',private_key=KEY,
        expected_signer=SIGNER,expected_ticket=service.ticket('123456'),keep_operating=True)
    run=started['id'];assert started['active'] and not case.sent
    emit();wait_for(lambda:service.snapshot(run)['status']=='WATCHING')
    assert len(case.sent)==1
    settings['mark']=exit_mark
    done=wait_for(lambda:(item if not (item:=service.snapshot(run))['active'] else None))
    assert done['status']=='ENTRY_LIMIT',done
    saved=read_bot_grant(case.vault,run)
    assert saved['selections'][0]['settlement']['net_realized_pnl_usdt']=='-0.03'
    assert len(case.settings['reduction_posts'])==1 and not case.settings['positions']
    assert service.operational_status()['active'] is False
    case.vault.backup(tmp_path/'automatic-mainnet-cycle.db');case.vault.lock()


def activate(origin,source,service,*,entries=1,wait=120,total='50',loss='10'):
    from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy
    policy=MainnetBotPolicy('123456',Decimal('50'),Decimal('100'),Decimal(total),Decimal(loss),max_entries=entries,wait_seconds=wait)
    return service.start(origin.paper,origin.lab,source['session_id'],policy,api_key='synthetic-mainnet-api-key',private_key=KEY,
        expected_signer=SIGNER,expected_ticket=service.ticket('123456'),keep_operating=True)


def test_stop_while_waiting_revokes_before_any_signal_can_send(tmp_path):
    case,origin,source,emit,settings,service=service_case(tmp_path)
    run=activate(origin,source,service)['id'];service.stop(run)
    done=wait_for(lambda:(item if not (item:=service.snapshot(run))['active'] else None))
    emit();assert not case.sent and done['status']=='STOPPED' and not service.operational_status()['active']
    case.vault.lock()


def test_lost_entry_and_ioc_replies_are_recovered_without_duplicate_posts(tmp_path):
    case,origin,source,emit,settings,service=service_case(tmp_path)
    run=activate(origin,source,service)['id'];case.settings['lose']=True;case.settings['reduction_lost']=True
    emit();wait_for(lambda:service.snapshot(run)['status']=='WATCHING');settings['mark']='96.9'
    done=wait_for(lambda:(item if not (item:=service.snapshot(run))['active'] else None))
    assert done['status']=='ENTRY_LIMIT',done
    assert len(case.sent)==1 and len(case.settings['reduction_posts'])==1 and not case.settings['positions']
    case.vault.backup(tmp_path/'lost-automatic-cycle.db');case.vault.lock()


def test_two_cycles_release_only_their_own_observation_stop_and_keep_aggregate_limits(tmp_path):
    from tests.test_grvt_bot_continuous import next_paper_open
    case,origin,source,emit,settings,service=service_case(tmp_path)
    run=activate(origin,source,service,entries=2,wait=10800,total='100')['id'];emit()
    wait_for(lambda:service.snapshot(run)['status']=='WATCHING');settings['mark']='96.9'
    wait_for(lambda:service.snapshot(run)['completed_entries']==1 and service.snapshot(run)['status']=='WAITING_SIGNAL')
    settings['mark']='100'
    venue=SimpleNamespace(origin=origin,cap=SimpleNamespace(instrument='BTC_USDT_Perp'),clock_ns=lambda:case.clock[0],
        advance_bot_clock=lambda seconds:case.clock.__setitem__(0,case.clock[0]+int(seconds*1_000_000_000)))
    next_paper_open(venue,source)
    wait_for(lambda:len(case.sent)==2 and service.snapshot(run)['status']=='WATCHING');settings['mark']='96.9'
    done=wait_for(lambda:(item if not (item:=service.snapshot(run))['active'] else None))
    assert done['status']=='ENTRY_LIMIT' and done['completed_entries']==2,done
    assert done['entry_budget_reserved']=='100' and done['net_realized_pnl_usdt']=='-0.06'
    case.vault.backup(tmp_path/'two-automatic-mainnet-cycles.db');case.vault.lock()


@pytest.mark.parametrize('total,loss,terminal',[('50','10','BUDGET_LIMIT'),('100','0.01','LOSS_LIMIT')])
def test_automatic_cycle_stops_at_its_reserved_budget_or_settled_loss(tmp_path,total,loss,terminal):
    case,origin,source,emit,settings,service=service_case(tmp_path)
    run=activate(origin,source,service,entries=2,total=total,loss=loss)['id'];emit()
    wait_for(lambda:service.snapshot(run)['status']=='WATCHING');settings['mark']='96.9'
    done=wait_for(lambda:(item if not (item:=service.snapshot(run))['active'] else None))
    assert done['status']==terminal and done['completed_entries']==1,done
    assert done['net_realized_pnl_usdt']=='-0.03' and len(case.sent)==1
    case.vault.backup(tmp_path/'bounded-bot.db');case.vault.lock()


def test_expired_waiting_bot_cannot_trade_a_subsequent_signal_or_resume_after_reopen(tmp_path):
    from app.live_execution.grvt_mainnet_bot_service import GrvtMainnetBotService
    case,origin,source,emit,settings,service=service_case(tmp_path)
    run=activate(origin,source,service)['id'];saved=service.snapshot(run)
    case.clock[0]=saved['entry_deadline_ns']
    done=wait_for(lambda:(item if not (item:=service.snapshot(run))['active'] else None))
    assert done['status']=='EXPIRED' and not case.sent
    emit();case.vault.lock();case.vault.unlock(PASSWORD)
    cold=GrvtMainnetBotService(case.vault).snapshot(run)
    assert not cold['active'] and cold['status']=='EXPIRED' and cold['permission_remaining_seconds']==0
    assert not case.sent;case.vault.lock()
