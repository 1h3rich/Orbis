"""Diario cifrado real y HTTP sustituido sólo en la frontera MAINNET."""

from copy import deepcopy
from decimal import Decimal
import time
from types import SimpleNamespace

import httpx
import pytest

from app.portfolio.vault import RealVault
from app.live_execution.grvt_mainnet_bracket import MainnetBracketDraft,GrvtMainnetError
from tests.test_grvt_testnet_signer import KEY,SIGNER
from tests.test_grvt_testnet_bracket_submit import PASSWORD


def live_case(directory):
    from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient
    vault=RealVault(directory/'mainnet.db'); vault.unlock(PASSWORD,create=True)
    clock=[time.time_ns()]; calls=[]; sent=[]
    settings={'lose':False,'account_balance':'10000','account_equity':'10000','positions':[],
        'open_orders':[],'account_time':None,'instrument_hash':'123456789','ask':'100.01',
        'private_hook':None,'public_hook':None,'ack_fault':None,'orders':{},'histories':{},'history_pages':{},'history_requests':[],
        'cancel_posts':[],'cancel_lost':False,'cancel_ignored':False,'cancel_ack':True,'initial_margin':'0','maintenance_margin':'0','unrealized_pnl':'0',
        'reduction_posts':[],'reduction_lost':False,'reduction_fill_size':None,'reduction_fill_price':'100','format_echo':False}
    settings.update(order_lookup_errors={},order_history=None)
    def response(url,payload,**kwargs): return httpx.Response(200,json=payload,request=httpx.Request('POST',url),**kwargs)
    def echo(order):
        order=deepcopy(order)
        if settings['format_echo']:
            for leg in order['legs']:
                for field in ('size','limit_price'):leg[field]=format(Decimal(leg[field]),'.9f')
            order['builder_fee']='0.000000000';order['signature']['signer']=order['signature']['signer'].lower()
            if 'trigger' in order['metadata']:
                trigger=order['metadata']['trigger']['tpsl'];trigger['trigger_price']=format(Decimal(trigger['trigger_price']),'.9f')
        return order
    def public(url,body):
        calls.append(url)
        if settings['public_hook']: settings['public_hook'](url,body)
        assert url.startswith('https://market-data.grvt.io/')
        if url.endswith('/instrument'):
            return response(url,{'result':{'instrument':'BTC_USDT_Perp','kind':'PERPETUAL','venues':['ORDERBOOK'],
                'instrument_hash':settings['instrument_hash'],'base_decimals':3,'tick_size':'0.01','min_size':'0.001','min_notional':'10'}})
        if url.endswith('/margin_rules'):
            return response(url,{'instrument':'BTC_USDT_Perp','max_position_size':'10',
                'risk_brackets':[{'tier':1,'notional_floor':'0','notional_cap':None,'maintenance_margin_rate':'0.01',
                    'initial_margin_rate':'0.02','max_leverage':50,'cumulative_maintenance_amount':'0'}]})
        assert url.endswith('/ticker')
        return response(url,{'result':{'instrument':'BTC_USDT_Perp','event_time':str(clock[0]),'best_bid_price':'99.99',
            'best_ask_price':settings['ask'],'best_bid_size':'10','best_ask_size':'10'}})
    def private(url,body,headers):
        calls.append(url)
        if settings['private_hook']: settings['private_hook'](url,body)
        if url=='https://edge.grvt.io/auth/api_key/login':
            return response(url,{'status':'success','sub_account_id':'123456'},
                headers={'set-cookie':'gravity=synthetic-mainnet-session; Path=/','x-grvt-account-id':'0x'+'2'*40})
        assert url.startswith('https://trades.grvt.io/')
        if url.endswith('/account_summary'):
            return response(url,{'result':{'sub_account_id':'123456','event_time':str(settings['account_time'] or clock[0]),'settle_currency':'USDT',
                'margin_type':'SIMPLE_CROSS_MARGIN','sub_account_mode':'SINGLE_ASSET_MODE','total_equity':settings['account_equity'],
                'initial_margin':settings['initial_margin'],'maintenance_margin':settings['maintenance_margin'],
                'unrealized_pnl':settings['unrealized_pnl'],'positions':settings['positions'],
                'spot_balances':[{'currency':'USDT','balance':settings['account_balance']}]}})
        if url.endswith('/open_orders'): return response(url,{'result':settings['open_orders']})
        if url.endswith('/order'):
            error = settings['order_lookup_errors'].get(body['client_order_id'])
            if error is not None:
                status,code = error
                return httpx.Response(status,json={'code':code},request=httpx.Request('POST',url))
            return response(url,{'result':echo(settings['orders'][body['client_order_id']])})
        if url.endswith('/order_history') and settings['order_history'] is not None:
            settings['history_requests'].append(('order_history',deepcopy(body)))
            return response(url,settings['order_history'](body))
        if url.rsplit('/',1)[-1] in ('fill_history','funding_payment_history','transfer_history',
                'withdrawal_history','deposit_history','interest_payment_history'):
            name=url.rsplit('/',1)[-1]; settings['history_requests'].append((name,deepcopy(body)))
            pages=settings['history_pages'].get(name)
            payload=pages[body['cursor']] if pages is not None else {'result':settings['histories'].get(name,[]),'next':''}
            return response(url,deepcopy(payload))
        assert url=='https://trades.grvt.io/full/v2/bulk_orders'
        if body['orders']==[]:
            assert set(body)=={'sub_account_id','orders','order_i_ds','client_order_i_ds','time_to_live_ms'}
            settings['cancel_posts'].append(deepcopy(body));parent=body['client_order_i_ds'][0]
            from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
            probe=RealVault(vault.path);probe.unlock(PASSWORD)
            try:assert read_mainnet_recovery(probe,parent)['cancel_claimed'] is True
            finally:probe.lock()
            if not settings['cancel_ignored']:
                for cid in (parent,str(int(parent)^1),str(int(parent)^2)):
                    raw=settings['orders'][cid]
                    if raw['state']['status'] in ('PENDING','OPEN'):
                        raw['state'].update(status='CANCELLED',update_time=str(clock[0]))
            if settings['cancel_lost']:raise httpx.ReadTimeout('Synthetic lost mainnet cancel reply')
            return response(url,{'result':{'orders':[],'cancel_acks':[{'ack':settings['cancel_ack']}]}})
        if len(body['orders'])==1:
            from decimal import localcontext
            from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
            settings['reduction_posts'].append(deepcopy(body));order=body['orders'][0]
            probe=RealVault(vault.path);probe.unlock(PASSWORD)
            try:
                view=read_mainnet_recovery(probe,settings['active_parent'])
                assert view['reductions'][-1]['claim']['client_order_id']==order['metadata']['client_order_id']
            finally:probe.lock()
            with localcontext() as context:
                context.prec=100
                size=Decimal(order['legs'][0]['size']);filled=Decimal(settings['reduction_fill_size'] or str(size))
                held=Decimal(settings['positions'][0]['size']);price=Decimal(settings['reduction_fill_price'])
                pnl=(price-100)*filled*(1 if held>0 else -1);fee=Decimal('0.01') if filled else Decimal(0)
                balance=Decimal(settings['account_balance'])+pnl-fee
                remaining=held+filled*(1 if order['legs'][0]['is_buying_asset'] else -1)
                settings['account_balance']=settings['account_equity']=str(balance)
                settings['positions']=[{'sub_account_id':'123456','instrument':'BTC_USDT_Perp','size':str(remaining)}] if remaining else []
                if not remaining:settings['initial_margin']=settings['maintenance_margin']=settings['unrealized_pnl']='0'
            raw={**deepcopy(order),'order_id':'0xmainnetreduce'+str(len(settings['reduction_posts'])),
                'metadata':{**deepcopy(order['metadata']),'create_time':str(clock[0])},
                'state':{'status':'FILLED' if filled==size else 'CANCELLED','traded_size':[str(filled)],'update_time':str(clock[0])}}
            settings['orders'][order['metadata']['client_order_id']]=raw
            if filled:
                settings['histories'].setdefault('fill_history',[]).append({'sub_account_id':'123456','instrument':'BTC_USDT_Perp',
                    'trade_id':'ioc-fill-'+str(len(settings['reduction_posts'])),'order_id':raw['order_id'],
                    'client_order_id':order['metadata']['client_order_id'],'event_time':str(clock[0]),
                    'is_buyer':order['legs'][0]['is_buying_asset'],'is_taker':True,'size':str(filled),'price':str(price),
                    'fee':str(fee),'fee_currency':'USDT','realized_pnl':str(pnl),'builder_fee':'0','is_liquidation':False})
            if settings['reduction_lost']:raise httpx.ReadTimeout('Synthetic lost mainnet reduction reply')
            return response(url,{'result':{'orders':[echo(raw)],'cancel_acks':[]}})
        sent.append(deepcopy(body))
        settings['active_parent']=body['orders'][0]['metadata']['client_order_id']
        for index,order in enumerate(body['orders']):
            settings['orders'][order['metadata']['client_order_id']]={**deepcopy(order),
                'order_id':'0xmainnetleg'+str(index)+'g'+str(len(sent)),'metadata':{**deepcopy(order['metadata']),'create_time':str(clock[0])},
                'state':{'status':'FILLED' if index==0 else 'OPEN','traded_size':[order['legs'][0]['size'] if index==0 else '0'],
                    'update_time':str(clock[0])}}
        from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
        # Otra conexión cifrada comprueba que el claim ya está COMMITted,
        # no sólo visible dentro de la transacción que va a emitir el POST.
        probe=RealVault(vault.path); probe.unlock(PASSWORD)
        try: before=read_mainnet_bracket(probe,body['orders'][0]['metadata']['client_order_id'])
        finally: probe.lock()
        assert before['status']=='POST_CLAIMED' and before['acknowledgement'] is None
        if settings['lose']: raise httpx.ReadTimeout('synthetic lost mainnet reply')
        return response(url,{'result':{'orders':[{**echo(order),'order_id':'0xmainnetleg'+str(index)+'g'+str(len(sent))}
            for index,order in enumerate(body['orders'])],'cancel_acks':[]}})
    factory=lambda:GrvtMainnetClient('synthetic-mainnet-api-key',http_post=private,clock_ns=lambda:clock[0])
    draft=MainnetBracketDraft('123456','BTC_USDT_Perp','LONG',Decimal('0.5'),Decimal('100'),
        Decimal('95'),Decimal('110'),Decimal('100'),Decimal('100'))
    return SimpleNamespace(vault=vault,clock=clock,calls=calls,sent=sent,public=public,factory=factory,draft=draft,settings=settings)


def arguments(case):
    return dict(client_factory=case.factory,public_post=case.public,private_key=KEY,expected_signer=SIGNER,
        clock_ns=lambda:case.clock[0],require_permission=lambda:None,
        confirmation='ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET')


def test_a_mainnet_protected_entry_has_a_durable_separate_claim_before_its_only_post(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=live_case(tmp_path); vault=case.vault; calls=case.calls; sent=case.sent; draft=case.draft
    parent=str(2**63+900)
    args=arguments(case)
    result=submit_mainnet_bracket_once(vault,draft,parent_client_order_id=parent,**args)
    assert result['network']=='MAINNET' and result['status']=='ACKNOWLEDGED'
    assert result['execution_unverified'] is True and result['position_verified'] is False
    assert len(sent)==1 and len(sent[0]['orders'])==3
    assert set(sent[0])=={'sub_account_id','orders','order_i_ds','client_order_i_ds'}
    assert all(row['signature']['chain_id']=='325' for row in sent[0]['orders'])
    assert len(result['acknowledgement']['orders'])==3
    count=len(calls)
    with pytest.raises(GrvtMainnetError): submit_mainnet_bracket_once(vault,draft,parent_client_order_id=parent,**args)
    assert len(sent)==1 and len(calls)==count
    with vault.session() as db:
        assert db.connection().exec_driver_sql('SELECT count(*) FROM grvt_testnet_bracket_groups').scalar_one()==0
    vault.backup(tmp_path/'mainnet-entry-backup.db'); vault.lock()
    vault.unlock(PASSWORD)
    assert read_mainnet_bracket(vault,parent)==result
    for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key','synthetic-mainnet-session'):
        assert secret not in str(result)
    vault.lock()


def test_lost_mainnet_reply_keeps_the_claim_and_blocks_another_id_after_reopen(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=live_case(tmp_path); case.settings['lose']=True; parent=str(2**63+901)
    with pytest.raises(GrvtMainnetError,match='no confirmado'):
        submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    stored=read_mainnet_bracket(case.vault,parent)
    assert stored['status']=='POST_CLAIMED' and stored['acknowledgement'] is None
    assert len(case.sent)==1 and stored['execution_unverified'] is True
    case.vault.backup(tmp_path/'uncertain-mainnet.db'); case.vault.lock(); case.vault.unlock(PASSWORD)
    count=len(case.calls)
    for cid in (parent,str(2**63+902)):
        with pytest.raises(GrvtMainnetError):
            submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=cid,**arguments(case))
    assert len(case.sent)==1 and len(case.calls)==count
    assert read_mainnet_bracket(case.vault,parent)==stored
    case.vault.lock()


def test_local_preflight_observations_cannot_be_after_the_claim_time(tmp_path):
    from app.live_execution.grvt_mainnet_preflight import collect_mainnet_preflight,verify_mainnet_preflight
    case=live_case(tmp_path)
    proof=collect_mainnet_preflight(case.factory(),case.draft,public_post=case.public,clock_ns=lambda:case.clock[0])
    for state in proof['states']: state['observed_at_ns']+=1
    proof['quote']['observed_at_ns']+=1
    with pytest.raises(GrvtMainnetError): verify_mainnet_preflight(proof,case.draft,now_ns=case.clock[0])
    assert not case.sent
    case.vault.lock()


def test_lost_mainnet_entry_can_be_observed_by_its_three_original_ids_without_resubmitting(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=live_case(tmp_path); case.settings['lose']=True; parent=str(2**63+903)
    with pytest.raises(GrvtMainnetError):
        submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    case.clock[0]+=1_000_000_000
    actual=observe_mainnet_bracket(case.vault,parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert actual['state']['entry_fully_filled'] is True
    assert actual['state']['account_position_verified'] is False
    assert actual['state']['residual_size_from_orders']=='0.5'
    saved=read_mainnet_bracket(case.vault,parent)
    assert saved['status']=='OBSERVED' and saved['acknowledgement'] is None
    assert saved['last_observation']==actual
    assert len(case.sent)==1
    case.vault.backup(tmp_path/'observed-mainnet.db'); case.vault.lock(); case.vault.unlock(PASSWORD)
    assert read_mainnet_bracket(case.vault,parent)==saved
    assert 'signature' not in str(actual) and SIGNER not in str(actual)
    case.vault.lock()


@pytest.mark.parametrize('fault', ['equity','positions','open_orders','stale','spread','instrument_changed'])
def test_mainnet_risk_rejections_do_not_reserve_an_entry_or_transmit_a_group(tmp_path,fault):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=live_case(tmp_path); parent=str(2**63+904)
    if fault=='equity': case.settings['account_equity']='9999'
    if fault=='positions': case.settings['positions']=[{'instrument':'ETH_USDT_Perp','size':'1'}]
    if fault=='open_orders': case.settings['open_orders']=[{'order_id':'unknown'}]
    if fault=='stale': case.settings['account_time']=case.clock[0]-6_000_000_000
    if fault=='spread': case.settings['ask']='101'
    if fault=='instrument_changed':
        count=[0]
        def hook(url,body):
            if url.endswith('/instrument'):
                count[0]+=1
                if count[0]>1: case.settings['instrument_hash']='987654321'
        case.settings['public_hook']=hook
    with pytest.raises(GrvtMainnetError):
        submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    assert not case.sent
    with pytest.raises(GrvtMainnetError,match='no encontrado'): read_mainnet_bracket(case.vault,parent)
    case.vault.lock()


def test_mainnet_stop_during_preflight_persists_without_sending_or_allocating(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import stop_mainnet_subaccount,read_mainnet_bracket
    case=live_case(tmp_path); parent=str(2**63+905); stopped=[]
    def hook(url,body):
        if url.endswith('/open_orders') and not stopped:
            stopped.append(stop_mainnet_subaccount(case.vault,case.draft.sub_account_id))
    case.settings['private_hook']=hook
    with pytest.raises(GrvtMainnetError):
        submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    assert stopped[0]['stopped'] is True and not case.sent
    with pytest.raises(GrvtMainnetError,match='no encontrado'): read_mainnet_bracket(case.vault,parent)
    case.vault.lock(); case.vault.unlock(PASSWORD)
    calls=len(case.calls)
    with pytest.raises(GrvtMainnetError):
        submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    assert len(case.calls)==calls and not case.sent
    case.vault.backup(tmp_path/'mainnet-stopped.db'); case.vault.lock()


def test_ram_revocation_after_durable_claim_never_sends_or_releases_the_reserved_entry(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=live_case(tmp_path); parent=str(2**63+906)
    def permission():
        try: read_mainnet_bracket(case.vault,parent)
        except GrvtMainnetError: return
        raise GrvtMainnetError('Synthetic RAM permission revoked')
    args=arguments(case); args['require_permission']=permission
    with pytest.raises(GrvtMainnetError):
        submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**args)
    assert not case.sent
    assert read_mainnet_bracket(case.vault,parent)['status']=='POST_CLAIMED'
    calls=len(case.calls)
    with pytest.raises(GrvtMainnetError):
        submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=str(2**63+910),**arguments(case))
    assert len(case.calls)==calls
    case.vault.backup(tmp_path/'mainnet-revoked.db'); case.vault.lock()


def test_mainnet_observation_rejects_a_changed_original_contract_and_preserves_prior_evidence(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=live_case(tmp_path); parent=str(2**63+907)
    submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    first=observe_mainnet_bracket(case.vault,parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    child=str(int(parent)^1)
    case.settings['orders'][child]['metadata']['trigger']['tpsl']['trigger_price']='96'
    with pytest.raises(GrvtMainnetError):
        observe_mainnet_bracket(case.vault,parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert read_mainnet_bracket(case.vault,parent)['last_observation']==first
    assert len(case.sent)==1
    case.vault.backup(tmp_path/'mainnet-contract-changed.db'); case.vault.lock()


def test_equivalent_remote_decimal_and_address_formatting_preserves_the_original_signed_contract(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
    case=live_case(tmp_path);case.settings['format_echo']=True;parent=str(2**63+9990)
    saved=submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    assert saved['status']=='ACKNOWLEDGED'
    observe_mainnet_bracket(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert len(case.sent)==1
    case.vault.backup(tmp_path/'mainnet-normalized-echo.db');case.vault.lock()
