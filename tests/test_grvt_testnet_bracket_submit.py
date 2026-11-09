"""Coordinación OTOCO: datos cifrados reales, sólo venue/tiempo sintéticos."""

from dataclasses import replace
from decimal import Decimal

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.portfolio.vault import RealVault
from tests.test_grvt_testnet_bracket import plan_for
from tests.test_grvt_testnet_bracket_journal import PASSWORD,source
from tests.test_grvt_testnet_signer import NOW,KEY,SIGNER,limits,metadata
from tests.test_grvt_testnet_risk import state,rules


def prepared(vault):
    from app.live_execution.grvt_testnet_bracket import compile_testnet_bracket,sign_testnet_bracket
    from app.live_execution.grvt_testnet_bracket_journal import prepare_bracket
    from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
    entry=replace(plan_for().entry,client_order_id=source()['client_order_id'],expiration_ns=NOW+10_000_000_000)
    quote=GrvtTestnetQuote(entry.instrument,NOW,NOW,Decimal('49999'),Decimal('50001'),
        Decimal('10'),Decimal('10'),last_price=Decimal('50000'))
    draft=compile_testnet_bracket(entry,state(),rules(),metadata(),limits(),limits(),quote,
        stop_loss=Decimal('49000'),take_profit=Decimal('53000'),max_spread_bps=25,max_slippage_bps=25,
        exit_nonces=(43,44),exit_expiration_ns=NOW+3_600_000_000_000,now_ns=NOW)
    orders=sign_testnet_bracket(draft,limits(),limits(),metadata(),dedicated_private_key=KEY,
        expected_signer=SIGNER,now_ns=NOW)
    plan=prepare_bracket(vault,orders,limits(),limits(),metadata(),signal_source=source(),now_ns=NOW)
    return plan,orders,quote


def test_claim_is_durable_one_use_and_preserves_risk_without_storing_signatures(tmp_path):
    from app.live_execution.grvt_testnet_bracket_journal import claim_bracket,read_bracket,abandon_bracket
    from app.live_execution.grvt_testnet_bracket_preflight import make_bracket_preflight
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    plan,orders,quote=prepared(vault)
    proof=make_bracket_preflight(plan,state(),rules(),quote,None,
        checked_at_ns=NOW,signal_recorded_at_ns=NOW)
    claimed=claim_bracket(vault,plan['parent_client_order_id'],orders,limits(),limits(),
        preflight=proof,now_ns=NOW+1)
    assert claimed['status']=='POST_CLAIMED' and claimed['preflight_available'] is True
    assert claimed['can_submit'] is False and claimed['protection_guaranteed'] is False
    for secret in (KEY.hex(),SIGNER,*[order['signature']['r'] for order in orders]):
        assert secret not in str(claimed)
    vault.lock(); vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD)
    assert read_bracket(vault,plan['parent_client_order_id'])==claimed
    with pytest.raises(GrvtTestnetBracketError):
        claim_bracket(vault,plan['parent_client_order_id'],orders,limits(),limits(),preflight=proof,now_ns=NOW+2)
    with pytest.raises(GrvtTestnetBracketError):
        abandon_bracket(vault,plan['parent_client_order_id'],now_ns=NOW+2)
    vault.backup(tmp_path/'claimed-backup.db'); vault.lock()


def live_group(tmp_path,direction='LONG',*,size='0.5',order_direction=None):
    import time
    from copy import deepcopy
    from app.live_execution.grvt_testnet_bracket import compile_testnet_bracket,sign_testnet_bracket
    from app.live_execution.grvt_testnet_order import AUTH_URL,BULK_ORDERS_URL,LOOKUP_URL,GrvtTestnetOrderClient,GrvtTestnetLimits
    from app.live_execution.grvt_testnet_signer import INSTRUMENT_URL,GrvtTestnetInstrument,GrvtTestnetLimitDraft
    from app.live_execution.grvt_testnet_quote import TESTNET_TICKER_URL,GrvtTestnetQuote
    from app.live_execution.grvt_testnet_risk import MARGIN_RULES_URL
    from tests.grvt_testnet_ui_fixture import GrvtTestnetScenario
    venue=GrvtTestnetScenario(tmp_path)
    origin=venue.seed(direction)
    entry_direction=order_direction or direction
    sub=origin['sub_account_id']; stamp=time.time_ns()
    instrument=GrvtTestnetInstrument('BTC_USDT_Perp',123456789,3,Decimal('0.01'),Decimal('0.001'),Decimal('10'))
    cap=GrvtTestnetLimits(sub,instrument.name,Decimal('100'))
    entry=GrvtTestnetLimitDraft(sub,instrument.name,'BUY' if entry_direction=='LONG' else 'SELL',Decimal(size),
        Decimal('100'),False,'0x'+'0'*40,origin['client_order_id'],42,stamp+10_000_000_000)
    current=replace(state(),sub_account_id=sub,event_time_ns=stamp,observed_at_ns=stamp)
    market_rules=replace(rules(),fetched_at_ns=stamp)
    quote=GrvtTestnetQuote(instrument.name,stamp,stamp,Decimal('99.99'),Decimal('100.01'),Decimal('10'),Decimal('10'),Decimal('100'))
    plan=compile_testnet_bracket(entry,current,market_rules,instrument,cap,cap,quote,
        stop_loss=Decimal('95' if entry_direction=='LONG' else '105'),take_profit=Decimal('110' if entry_direction=='LONG' else '90'),
        max_spread_bps=25,max_slippage_bps=25,exit_nonces=(43,44),exit_expiration_ns=stamp+3_600_000_000_000,now_ns=stamp)
    orders=sign_testnet_bracket(plan,cap,cap,instrument,dedicated_private_key=KEY,expected_signer=SIGNER,now_ns=stamp)
    vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    recorded={'sends':[],'public':[],'private':[],'lose':False,'stop_after_read':False,'account_reads':0,'clock_offset':0,'contracts':[],'venue_state':{}}
    def observed_ns(): return recorded.get('clock_ns',time.time_ns)()
    def public(url,body):
        recorded['public'].append(url)
        if url==INSTRUMENT_URL:
            return venue.response(url,{'result':{'instrument':instrument.name,'kind':'PERPETUAL','venues':['ORDERBOOK'],
                'instrument_hash':'123456789','base_decimals':3,'tick_size':'0.01','min_size':'0.001','min_notional':'10'}})
        if url==TESTNET_TICKER_URL:
            return venue.response(url,{'result':{'instrument':instrument.name,'event_time':str(observed_ns()),
                'best_bid_price':'99.99','best_ask_price':'100.01','best_bid_size':'10','best_ask_size':'10','last_price':'100'}})
        assert url==MARGIN_RULES_URL
        if recorded['stop_after_read']:
            from datetime import datetime,timezone
            from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
            stop_testnet_subaccount(vault,sub,now=datetime.now(timezone.utc))
        return venue.public_rules(url,body)
    def private(url,body,headers):
        recorded['private'].append(url)
        if url==AUTH_URL: return venue.response(url,{'status':'success','sub_account_id':sub})
        if url.endswith('/account_summary'):
            recorded['account_reads']+=1
            return venue.response(url,{'result':{'sub_account_id':sub,'event_time':str(observed_ns()),
                'settle_currency':'USDT','margin_type':'SIMPLE_CROSS_MARGIN','sub_account_mode':'SINGLE_ASSET_MODE',
                'total_equity':'10000','initial_margin':'0','maintenance_margin':'0','unrealized_pnl':'0',
                'positions':[],'spot_balances':[{'currency':'USDT','balance':'10000'}]}})
        if url.endswith('/open_orders'): return venue.response(url,{'result':[]})
        if url==LOOKUP_URL: return venue.response(url,{'result':deepcopy(recorded['venue_state'][body['client_order_id']])})
        assert url==BULK_ORDERS_URL
        recorded['sends'].append(url); recorded['contracts']=deepcopy(body['orders'])
        for index,order in enumerate(body['orders']):
            stamp=observed_ns()
            recorded['venue_state'][order['metadata']['client_order_id']]={**deepcopy(order),'order_id':'0xlinked'+str(len(recorded['sends']))+'leg'+str(index),
                'metadata':{**deepcopy(order['metadata']),'create_time':str(stamp)},
                'state':{'status':'FILLED' if index==0 else 'OPEN','update_time':str(stamp),
                    'traded_size':[order['legs'][0]['size'] if index==0 else '0']}}
        if recorded['lose']:
            import httpx
            raise httpx.ReadTimeout('synthetic lost bulk reply')
        return venue.response(url,{'result':{'orders':[{**deepcopy(order),'order_id':'0xlinked'+str(len(recorded['sends']))+'leg'+str(index)}
            for index,order in enumerate(body['orders'])],'cancel_acks':[]}})
    client=GrvtTestnetOrderClient('fake-testnet-key-'+sub,http_post=private)
    return venue,origin,vault,orders,cap,instrument,client,public,recorded


def close_paper_origin(venue,origin):
    return venue.observe_risk_mark(origin['session_id'],Decimal('110'))


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_real_paper_replay_leads_to_one_durable_linked_bulk_and_no_retry_after_reopen(tmp_path,direction):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path,direction)
    binding,result=submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    assert binding.action=='OPEN' and binding.client_order_id==origin['client_order_id']
    assert result['status']=='ACKNOWLEDGED' and len(result['acknowledged_orders'])==3
    assert len(recorded['sends'])==1 and recorded['account_reads']==2
    assert all('testnet.grvt.io' in url for url in recorded['private']+recorded['public'])
    vault.lock(); vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD)
    assert read_bracket(vault,origin['client_order_id'])==result
    with pytest.raises(GrvtTestnetBracketError):
        submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    assert len(recorded['sends'])==1
    vault.backup(tmp_path/'submitted-backup.db'); vault.lock()


def test_lost_bulk_reply_remains_claimed_and_cannot_be_resent_by_a_new_client(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_order import GrvtTestnetOrderError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    recorded['lose']=True
    def submit(vault,client):
        return submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    with pytest.raises(GrvtTestnetOrderError): submit(vault,client)
    assert read_bracket(vault,origin['client_order_id'])['status']=='POST_CLAIMED'
    vault.lock(); vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD)
    # Otro transporte sin cuota consumida no elude la concesión persistente.
    fresh=venue.factory('fake-testnet-key-'+origin['sub_account_id'])
    with pytest.raises(GrvtTestnetBracketError): submit(vault,fresh)
    assert len(recorded['sends'])==1
    vault.backup(tmp_path/'uncertain.db'); vault.lock()


@pytest.mark.parametrize('fault',['expired','stopped','wrong_side','corrupt_replay','closed_source'])
def test_invalid_paper_source_cannot_reserve_or_send_a_linked_entry(tmp_path,fault):
    import time
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_signal import GrvtTestnetSignalError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    clock=time.time_ns
    if fault=='expired': clock=lambda:time.time_ns()+21_000_000_000
    if fault=='stopped': venue.monitor.stop(origin['session_id'])
    if fault=='wrong_side':
        from app.live_execution.grvt_testnet_preflight import client_id_for_signal
        # Una señal válida diferente no autoriza la identidad firmada anterior.
        other=venue.seed('SHORT'); origin=other
        assert client_id_for_signal(other['session_id'],other['action_request_id'])!=orders[0]['metadata']['client_order_id']
    if fault=='corrupt_replay':
        with venue.paper.connect() as db:
            db.execute("UPDATE paper_signal_observations SET evidence_json='{}' WHERE session_id=?",(origin['session_id'],))
    if fault=='closed_source':
        assert close_paper_origin(venue,origin)=='EXECUTED'
    with pytest.raises((GrvtTestnetSignalError,GrvtTestnetBracketError)):
        submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public,clock_ns=clock)
    with pytest.raises(GrvtTestnetBracketError): read_bracket(vault,orders[0]['metadata']['client_order_id'])
    assert recorded['sends']==[] and recorded['public']==[]
    vault.lock()


def test_stop_arriving_during_risk_review_prevents_the_bulk(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    recorded['stop_after_read']=True
    with pytest.raises(GrvtTestnetBracketError):
        submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    assert not recorded['sends']
    vault.lock()


def test_closed_source_scenario_uses_current_observation_time_after_slow_setup(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_signal import GrvtTestnetSignalError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    venue.clock['now']-=30  # Reproduce lentitud del setup sin una espera real.
    assert close_paper_origin(venue,origin)=='EXECUTED'
    with pytest.raises(GrvtTestnetSignalError):
        submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    assert not recorded['sends'] and not recorded['public']
    vault.lock()


def test_unknown_send_is_recovered_by_three_reads_after_restart_without_original_signatures(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_order import GrvtTestnetOrderError,LOOKUP_URL
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    recorded['lose']=True
    with pytest.raises(GrvtTestnetOrderError):
        submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    vault.lock(); vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD)
    observed=observe_bracket_orders(vault,client,origin['client_order_id'])
    assert observed['scope']=='ORDER_STATE_ONLY' and observed['entry_fully_filled'] is True
    assert observed['residual_size_from_orders']=='0.5' and observed['account_position_verified'] is False
    assert observed['protection_guaranteed'] is False and observed['can_submit'] is False
    assert recorded['private'].count(LOOKUP_URL)==3 and len(recorded['sends'])==1
    for secret in (KEY.hex(),SIGNER,*[order['signature']['r'] for order in orders]): assert secret not in str(observed)
    vault.lock(); vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD)
    restored=read_bracket(vault,origin['client_order_id'])
    assert restored['last_observation']==observed and restored['status']=='POST_CLAIMED'
    vault.backup(tmp_path/'observed.db'); vault.lock()


def test_recovery_accepts_equivalent_decimal_representations_without_losing_the_signature(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path,size='0.500')
    submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    observed=observe_bracket_orders(vault,client,origin['client_order_id'])
    assert Decimal(observed['residual_size_from_orders'])==Decimal('0.5')
    assert observed['entry_fully_filled'] is True and len(recorded['sends'])==1
    vault.lock()


@pytest.mark.parametrize('fault',['stale_signal','stale_account','open_position','spread','trigger_reached','parent_outlives_signal','missing_proof'])
def test_claim_rejects_unverifiable_risk_or_signal_without_consuming_post(tmp_path,fault):
    from copy import deepcopy
    from app.live_execution.grvt_testnet_bracket_journal import claim_bracket,read_bracket
    from app.live_execution.grvt_testnet_bracket_preflight import make_bracket_preflight
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    plan,orders,quote=prepared(vault)
    proof=make_bracket_preflight(plan,state(),rules(),quote,None,checked_at_ns=NOW,signal_recorded_at_ns=NOW)
    if fault=='stale_signal': proof['signal_recorded_at_ns']=NOW-21_000_000_000
    if fault=='stale_account': proof['state']['event_time_ns']=NOW-6_000_000_000
    if fault=='open_position': proof['state']['positions']=[['BTC_USDT_Perp','0.1']]
    if fault=='spread': proof['quote']['ask']='51000'
    if fault=='trigger_reached': proof['quote']['last_price']='49000'
    if fault=='parent_outlives_signal': proof['signal_recorded_at_ns']=NOW-11_000_000_000
    if fault=='missing_proof': proof=None
    with pytest.raises(GrvtTestnetBracketError):
        claim_bracket(vault,plan['parent_client_order_id'],orders,limits(),limits(),preflight=deepcopy(proof),now_ns=NOW+1)
    assert read_bracket(vault,plan['parent_client_order_id'])['status']=='PREPARED'
    vault.lock()


def test_expiration_between_claim_and_network_leaves_consumed_attempt_with_no_bulk(tmp_path):
    import time
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_signal import GrvtTestnetSignalError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    def clock():
        try: claimed=read_bracket(vault,origin['client_order_id'])['status']=='POST_CLAIMED'
        except GrvtTestnetBracketError: claimed=False
        return time.time_ns()+(21_000_000_000 if claimed else 0)
    with pytest.raises(GrvtTestnetSignalError):
        submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public,clock_ns=clock)
    assert read_bracket(vault,origin['client_order_id'])['status']=='POST_CLAIMED'
    assert recorded['sends']==[]
    vault.backup(tmp_path/'expired-after-claim.db'); vault.lock()


def test_partial_fok_is_saved_as_actual_partial_and_applies_persistent_stop(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    recorded['venue_state'][origin['client_order_id']]['state'].update(status='CANCELLED',traded_size=['0.2'])
    result=observe_bracket_orders(vault,client,origin['client_order_id'])
    assert result['fok_partial_violation'] is True and result['residual_size_from_orders']=='0.2'
    assert result['needs_review'] is True and result['protection_guaranteed'] is False
    assert read_testnet_recovery(vault,cap.sub_account_id).stopped is True
    vault.lock(); vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD)
    assert read_testnet_recovery(vault,cap.sub_account_id).stopped is True
    vault.backup(tmp_path/'partial-fok.db'); vault.lock()


def test_removing_saved_observation_is_detected_even_if_the_rest_of_the_chain_is_valid(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.portfolio.vault import VaultError
    from app.portfolio.models import GrvtTestnetBracketObservation
    from sqlalchemy import delete
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    observe_bracket_orders(vault,client,origin['client_order_id'])
    with vault.session() as db:
        db.execute(delete(GrvtTestnetBracketObservation)); db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_bracket(vault,origin['client_order_id'])
    with pytest.raises(VaultError): vault.backup(tmp_path/'truncated.db')
    vault.lock()


def test_v37_preparation_migrates_with_encrypted_preimage_and_all_three_reservations(tmp_path):
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    plan,_,_=prepared(vault)
    with vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bracket_observations')
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bracket_claims')
        db.connection().exec_driver_sql('PRAGMA user_version = 37'); db.commit()
    before=read_bracket(vault,plan['parent_client_order_id']); vault.lock()
    vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD)
    assert read_bracket(vault,plan['parent_client_order_id'])==before
    assert vault.last_migration_backup.read_bytes()[:16]!=b'SQLite format 3\x00'
    with vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
    vault.backup(tmp_path/'migrated-v38.db'); vault.lock()


def test_a_valid_short_signature_does_not_turn_a_long_paper_signal_into_a_short(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_signal import GrvtTestnetSignalError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path,order_direction='SHORT')
    with pytest.raises(GrvtTestnetSignalError):
        submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    assert not recorded['public'] and not recorded['sends']
    vault.lock()


def test_existing_ioc_signal_interface_still_rejects_the_fok_parent(tmp_path):
    from app.live_execution.grvt_testnet_signal import submit_signal_testnet_once
    from app.live_execution.grvt_testnet_order import GrvtTestnetOrderError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    with pytest.raises(GrvtTestnetOrderError):
        submit_signal_testnet_once(venue.paper,venue.lab,vault,client,orders[0],cap,
            session_id=origin['session_id'],action_request_id=origin['action_request_id'])
    assert not recorded['sends']
    vault.lock()


def test_two_vault_connections_cannot_both_claim_the_same_group(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.live_execution.grvt_testnet_bracket_journal import claim_bracket,read_bracket
    from app.live_execution.grvt_testnet_bracket_preflight import make_bracket_preflight
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    first=RealVault(tmp_path/'real.db'); first.unlock(PASSWORD,create=True)
    plan,orders,quote=prepared(first)
    second=RealVault(tmp_path/'real.db'); second.unlock(PASSWORD)
    proof=make_bracket_preflight(plan,state(),rules(),quote,None,checked_at_ns=NOW,signal_recorded_at_ns=NOW)
    def claim(vault):
        try:
            claim_bracket(vault,plan['parent_client_order_id'],orders,limits(),limits(),preflight=proof,now_ns=NOW+1)
            return 'CLAIMED'
        except GrvtTestnetBracketError: return 'CONSUMED'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim,(first,second)))==['CLAIMED','CONSUMED']
    assert read_bracket(first,plan['parent_client_order_id'])['status']=='POST_CLAIMED'
    first.backup(tmp_path/'one-claim.db'); first.lock(); second.lock()


@pytest.mark.parametrize('fault',['exchange_id','trigger','expiry_number','regressed_fill'])
def test_altered_or_regressing_venue_state_never_replaces_verified_history(tmp_path,fault):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    before=observe_bracket_orders(vault,client,origin['client_order_id'])
    actual=recorded['venue_state'][origin['client_order_id']]
    if fault=='exchange_id': actual['order_id']='0xforeign'
    if fault=='expiry_number': actual['signature']['expiration']=float(actual['signature']['expiration'])
    if fault=='regressed_fill': actual['state']['traded_size']=['0.4']
    if fault=='trigger':
        child=recorded['venue_state'][orders[1]['metadata']['client_order_id']]
        child['metadata']['trigger']['tpsl']['trigger_price']='94'
    with pytest.raises(GrvtTestnetBracketError): observe_bracket_orders(vault,client,origin['client_order_id'])
    assert read_bracket(vault,origin['client_order_id'])['last_observation']==before
    assert len(recorded['sends'])==1
    vault.backup(tmp_path/'verified-before-bad-view.db'); vault.lock()


def test_rehashing_a_false_protection_claim_cannot_make_stored_observation_valid(tmp_path):
    import json
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_oco import _encode
    from app.portfolio.models import GrvtTestnetBracketClaim,GrvtTestnetBracketObservation
    from app.portfolio.vault import VaultError
    from sqlalchemy import select
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    observe_bracket_orders(vault,client,origin['client_order_id'])
    with vault.session() as db:
        row=db.scalar(select(GrvtTestnetBracketObservation))
        payload=json.loads(row.payload_json); payload['state']['protection_guaranteed']=True
        row.payload_json,row.payload_digest=_encode(payload)
        db.get(GrvtTestnetBracketClaim,origin['client_order_id']).observation_head_digest=row.payload_digest
        db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_bracket(vault,origin['client_order_id'])
    with pytest.raises(VaultError): vault.backup(tmp_path/'false-protection.db')
    vault.lock()
