"""Puente causal LAB/PAPER → MAINNET; sólo HTTP externo es controlado."""

from app.portfolio.vault import SCHEMA_VERSION
import time
from decimal import Decimal
from pathlib import Path
import pytest
from tests.grvt_testnet_ui_fixture import GrvtTestnetScenario
from tests.test_grvt_mainnet_submit import live_case,PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER


def bot_case(directory,direction='LONG',*,research=False):
    source_dir=directory/'source';source_dir.mkdir()
    origin=GrvtTestnetScenario(source_dir);source=origin.seed(direction,defer_open=True,research=research)
    case=live_case(directory);case.clock[0]=time.time_ns()
    def emit():
        case.clock[0]=time.time_ns()+1_000_000
        origin.clock['now']=case.clock[0]//1_000_000_000
        origin.monitor.clock_ns=lambda:case.clock[0]
        origin.monitor.poll(source['session_id'])
    return case,origin,source,emit


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_future_causal_signal_is_reserved_before_the_only_signed_mainnet_post(tmp_path,direction):
    from app.live_execution.grvt_mainnet_bot_entry import (
        MainnetBotPolicy,begin_bot_grant,read_bot_grant,submit_bot_entry_once)
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    case,origin,source,emit=bot_case(tmp_path,direction)
    policy=MainnetBotPolicy('123456',Decimal('50'),Decimal('100'),Decimal('100'),Decimal('10'),max_entries=2)
    grant=begin_bot_grant(case.vault,origin.paper,origin.lab,source['session_id'],policy,now_ns=case.clock[0])
    assert grant['authorization'] is False and not case.sent and not grant['selections']
    emit()
    args={'client_factory':case.factory,'private_key':KEY,'expected_signer':SIGNER,'require_permission':lambda:None,
        'public_post':case.public,'clock_ns':lambda:case.clock[0]}
    result=submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**args)
    parent=result['parent_client_order_id']
    assert result['claim']['source']=='BOT_NATIVE_PROTECTED_ENTRY' and len(case.sent)==1
    assert result['claim']['draft']['direction']==direction
    assert all(order['signature']['chain_id']=='325' for order in case.sent[0]['orders'])
    saved=read_bot_grant(case.vault,grant['id'])
    assert saved['selections'][0]['parent_client_order_id']==parent and saved['source']['dataset_hash']
    assert saved['selections'][0]['action_request_id']
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**args)
    assert len(case.sent)==1
    case.vault.backup(tmp_path/'mainnet-bot.db');case.vault.lock();case.vault.unlock(PASSWORD)
    assert read_bot_grant(case.vault,grant['id'])==saved
    assert saved['authorization'] is False
    case.vault.lock()


def grant_case(directory):
    from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy,begin_bot_grant
    case,origin,source,emit=bot_case(directory)
    policy=MainnetBotPolicy('123456',Decimal('50'),Decimal('100'),Decimal('100'),Decimal('10'),max_entries=2)
    grant=begin_bot_grant(case.vault,origin.paper,origin.lab,source['session_id'],policy,now_ns=case.clock[0])
    return case,origin,source,emit,grant,policy


def entry_args(case,**change):
    return {'client_factory':case.factory,'private_key':KEY,'expected_signer':SIGNER,'require_permission':lambda:None,
        'public_post':case.public,'clock_ns':lambda:case.clock[0],**change}


def test_waiting_grant_excludes_manual_orders_and_a_second_grant_before_any_network_call(tmp_path):
    from app.live_execution.grvt_mainnet_bot_entry import begin_bot_grant
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    from tests.test_grvt_mainnet_submit import arguments
    case,origin,source,emit,grant,policy=grant_case(tmp_path)
    with pytest.raises(GrvtMainnetError):begin_bot_grant(case.vault,origin.paper,origin.lab,source['session_id'],policy,now_ns=case.clock[0])
    with pytest.raises(GrvtMainnetError):submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=str(2**63+991),**arguments(case))
    assert not case.calls and not case.sent
    case.vault.lock()


def test_revocation_after_durable_selection_burns_the_signal_without_a_private_call(tmp_path):
    from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant,submit_bot_entry_once
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    from app.portfolio.vault import RealVault
    case,origin,source,emit,grant,policy=grant_case(tmp_path);emit()
    observer=RealVault(case.vault.path);observer.unlock(PASSWORD)
    def permission():
        if read_bot_grant(observer,grant['id'])['selections']:raise GrvtMainnetError('Synthetic revocation after durable selection')
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case,require_permission=permission))
    assert len(read_bot_grant(case.vault,grant['id'])['selections'])==1
    assert not case.sent and not any('trades.grvt.io' in call or 'edge.grvt.io' in call for call in case.calls)
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    assert not case.sent
    observer.lock();case.vault.lock()


def test_lost_mainnet_reply_does_not_reselect_or_resend_after_reopen(tmp_path):
    from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant,submit_bot_entry_once
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case,origin,source,emit,grant,policy=grant_case(tmp_path);emit();case.settings['lose']=True
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    saved=read_bot_grant(case.vault,grant['id']);parent=saved['selections'][0]['parent_client_order_id']
    case.vault.backup(tmp_path/'uncertain-bot.db');case.vault.lock();case.vault.unlock(PASSWORD)
    assert read_mainnet_bracket(case.vault,parent)['status']=='POST_CLAIMED'
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    assert len(case.sent)==1 and read_bot_grant(case.vault,grant['id'])==saved
    case.vault.lock()


def test_a_signal_older_than_the_grant_or_failing_causal_replay_cannot_reach_grvt(tmp_path):
    from app.live_execution.grvt_mainnet_bot_entry import submit_bot_entry_once,read_bot_grant
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    case,origin,source,emit,grant,policy=grant_case(tmp_path)
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    emit()
    with origin.paper.connect() as db:
        db.execute("UPDATE paper_signal_observations SET entry_match=0 WHERE session_id=?",(source['session_id'],));db.commit()
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    assert not case.calls and not read_bot_grant(case.vault,grant['id'])['selections']
    case.vault.lock()


def test_unreconciled_entry_cannot_be_called_complete_or_free_the_grant(tmp_path):
    from app.live_execution.grvt_mainnet_bot_entry import submit_bot_entry_once,finish_bot_grant,read_bot_grant
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    case,origin,source,emit,grant,policy=grant_case(tmp_path);emit()
    submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    with pytest.raises(GrvtMainnetError):finish_bot_grant(case.vault,grant['id'],status='COMPLETE',now_ns=case.clock[0])
    stopped=finish_bot_grant(case.vault,grant['id'],status='STOPPED',now_ns=case.clock[0])
    assert stopped['terminal']['status']=='STOPPED' and stopped['selections'][0]['settlement'] is None
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    assert len(case.sent)==1
    case.vault.backup(tmp_path/'stopped-bot-with-position.db');case.vault.lock()


def close_remote_group(case,parent,*,losing=False):
    from decimal import localcontext
    entry=case.settings['orders'][parent];stop=case.settings['orders'][str(int(parent)^1)];target=case.settings['orders'][str(int(parent)^2)]
    case.clock[0]+=1_000_000_000;stamp=case.clock[0]
    exit_order,other=(stop,target) if losing else (target,stop)
    other['state'].update(status='CANCELLED',update_time=str(stamp))
    size=entry['legs'][0]['size'];exit_order['state'].update(status='FILLED',traded_size=[size],update_time=str(stamp))
    with localcontext() as context:
        context.prec=100
        pnl=(Decimal(exit_order['legs'][0]['limit_price'])-Decimal(entry['legs'][0]['limit_price']))*Decimal(size)*(1 if entry['legs'][0]['is_buying_asset'] else -1)
        net=pnl-Decimal('0.05');balance=Decimal(case.settings['account_balance'])+net
    def fill(order,trade,at,fee,realized):
        return {'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':trade,'order_id':order['order_id'],
            'client_order_id':order['metadata']['client_order_id'],'event_time':str(at),'is_buyer':order['legs'][0]['is_buying_asset'],
            'is_taker':True,'size':size,'price':order['legs'][0]['limit_price'],'fee':fee,'fee_currency':'USDT',
            'realized_pnl':str(realized),'builder_fee':'0','is_liquidation':False}
    case.settings['histories']['fill_history']=[fill(entry,'bot-entry-'+parent,int(entry['metadata']['create_time']),'0.02',0),
        fill(exit_order,'bot-exit-'+parent,stamp,'0.03',pnl)]
    case.settings.update(account_balance=str(balance),account_equity=str(balance),positions=[])
    return str(net)


@pytest.mark.parametrize('losing,total,loss,terminal',[(False,'50','10','BUDGET_LIMIT'),(True,'100','1','LOSS_LIMIT')])
def test_only_a_real_financial_receipt_can_settle_a_cycle_and_stop_at_aggregate_limits(tmp_path,losing,total,loss,terminal):
    from dataclasses import replace
    from app.live_execution.grvt_mainnet_bot_entry import (MainnetBotPolicy,begin_bot_grant,submit_bot_entry_once,settle_bot_selection,
        finish_bot_grant,read_bot_grant)
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    case,origin,source,emit=bot_case(tmp_path)
    policy=MainnetBotPolicy('123456',Decimal('50'),Decimal('100'),Decimal(total),Decimal(loss),max_entries=2)
    grant=begin_bot_grant(case.vault,origin.paper,origin.lab,source['session_id'],policy,now_ns=case.clock[0]);emit()
    group=submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case));parent=group['parent_client_order_id']
    with pytest.raises(GrvtMainnetError):settle_bot_selection(case.vault,grant['id'],now_ns=case.clock[0])
    expected=close_remote_group(case,parent,losing=losing)
    for _ in range(2):
        audit_mainnet_bracket(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.clock[0]+=6_000_000_000
    receipt=reconcile_mainnet_bracket(case.vault,parent,now_ns=case.clock[0])
    saved=settle_bot_selection(case.vault,grant['id'],now_ns=case.clock[0])
    assert Decimal(saved['selections'][0]['settlement']['net_realized_pnl_usdt'])==Decimal(expected)
    assert settle_bot_selection(case.vault,grant['id'],now_ns=case.clock[0])==saved
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    done=finish_bot_grant(case.vault,grant['id'],status=terminal,now_ns=case.clock[0])
    assert done['terminal']['status']==terminal and len(case.sent)==1
    case.vault.backup(tmp_path/'settled-bot.db');case.vault.lock();case.vault.unlock(PASSWORD)
    assert read_bot_grant(case.vault,grant['id'])==done
    case.vault.lock()


@pytest.mark.parametrize('damage',['event','reservation'])
def test_backup_rejects_truncated_bot_selection_evidence(tmp_path,damage):
    from app.live_execution.grvt_mainnet_bot_entry import submit_bot_entry_once
    from app.portfolio.models import GrvtMainnetBotEvent,GrvtMainnetBotSelection
    from app.portfolio.vault import VaultError
    case,origin,source,emit,grant,policy=grant_case(tmp_path);emit()
    group=submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    with case.vault.session() as db:
        if damage=='event':db.delete(db.get(GrvtMainnetBotEvent,(grant['id'],1)))
        else:db.delete(db.get(GrvtMainnetBotSelection,group['parent_client_order_id']))
        db.commit()
    with pytest.raises(VaultError):case.vault.backup(tmp_path/'damaged-bot.db')
    case.vault.lock()


def test_v52_migration_preserves_a_manual_group_and_adds_no_bot_permission(tmp_path):
    from app.portfolio.vault import SCHEMA_VERSION
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    from tests.test_grvt_mainnet_submit import arguments
    case=live_case(tmp_path);parent=str(2**63+997)
    before=submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    with case.vault.session() as db:
        for table in ('grvt_mainnet_bot_selections','grvt_mainnet_bot_events','grvt_mainnet_bot_runs'):
            db.connection().exec_driver_sql('DROP TABLE '+table)
        db.connection().exec_driver_sql('PRAGMA user_version=52');db.commit()
    case.vault.lock();case.vault.unlock(PASSWORD)
    assert case.vault.last_migration_backup.is_file()
    assert read_mainnet_bracket(case.vault,parent)==before
    with case.vault.session() as db:
        assert db.connection().exec_driver_sql('SELECT count(*) FROM grvt_mainnet_bot_runs').scalar_one()==0
    case.vault.backup(tmp_path/'v53-migrated-mainnet.db');case.vault.lock()


@pytest.mark.parametrize('old',['BEFORE_GRANT','EXPIRED'])
def test_an_existing_or_expired_paper_signal_never_becomes_a_fresh_mainnet_entry(tmp_path,old):
    from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy,begin_bot_grant,submit_bot_entry_once,read_bot_grant
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    case,origin,source,emit=bot_case(tmp_path)
    policy=MainnetBotPolicy('123456',Decimal('50'),Decimal('100'),Decimal('100'),Decimal('10'),max_entries=2)
    if old=='BEFORE_GRANT':emit();case.clock[0]+=1_000_000
    grant=begin_bot_grant(case.vault,origin.paper,origin.lab,source['session_id'],policy,now_ns=case.clock[0])
    if old=='EXPIRED':emit();case.clock[0]+=21_000_000_000
    with pytest.raises(GrvtMainnetError):submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case))
    assert not case.calls and not read_bot_grant(case.vault,grant['id'])['selections']
    case.vault.lock()


def test_two_distinct_causal_signals_require_two_flat_receipts_and_obey_the_entry_count(tmp_path):
    from types import SimpleNamespace
    from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy,begin_bot_grant,submit_bot_entry_once,settle_bot_selection,finish_bot_grant
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    from tests.test_grvt_bot_continuous import next_paper_open
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    case,origin,source,emit=bot_case(tmp_path)
    policy=MainnetBotPolicy('123456',Decimal('50'),Decimal('100'),Decimal('100'),Decimal('10'),max_entries=2,wait_seconds=10800)
    grant=begin_bot_grant(case.vault,origin.paper,origin.lab,source['session_id'],policy,now_ns=case.clock[0]);emit()
    parents=[]
    def settle():
        group=submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case));parent=group['parent_client_order_id'];parents.append(parent)
        close_remote_group(case,parent)
        for _ in range(2):
            audit_mainnet_bracket(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
            case.clock[0]+=6_000_000_000
        reconcile_mainnet_bracket(case.vault,parent,now_ns=case.clock[0])
        return settle_bot_selection(case.vault,grant['id'],now_ns=case.clock[0])
    first=settle();assert len(first['selections'])==1
    venue=SimpleNamespace(origin=origin,cap=SimpleNamespace(instrument='BTC_USDT_Perp'),clock_ns=lambda:case.clock[0],
        advance_bot_clock=lambda seconds:case.clock.__setitem__(0,case.clock[0]+int(seconds*1_000_000_000)))
    next_paper_open(venue,source)
    # El HTTP controlado devuelve ahora sólo los fills de la nueva ventana,
    # igual que el filtro start_time del proveedor; el primer recibo no cambia.
    case.settings['histories']['fill_history']=[]
    second=settle();assert len(second['selections'])==2 and len(set(parents))==2
    assert all(item['settlement'] for item in second['selections']) and len(case.sent)==2
    with pytest.raises(GrvtMainnetError):finish_bot_grant(case.vault,grant['id'],status='LOSS_LIMIT',now_ns=case.clock[0])
    done=finish_bot_grant(case.vault,grant['id'],status='ENTRY_LIMIT',now_ns=case.clock[0])
    assert done['terminal']['status']=='ENTRY_LIMIT'
    case.vault.backup(tmp_path/'two-bot-cycles.db');case.vault.lock()


@pytest.mark.parametrize('manual',['MANUAL','UNKNOWN'])
def test_bot_cannot_auto_release_a_manual_or_unknown_stop_even_after_a_flat_receipt(tmp_path,manual):
    from app.live_execution.grvt_mainnet_bot_entry import submit_bot_entry_once,settle_bot_selection
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    from app.live_execution.grvt_mainnet_stop_origin import release_bot_stop
    from app.live_execution.grvt_mainnet_journal import stop_mainnet_subaccount,_control
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    from app.portfolio.models import GrvtMainnetStopOrigin
    case,origin,source,emit,grant,policy=grant_case(tmp_path);emit()
    group=submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case));parent=group['parent_client_order_id']
    stop_mainnet_subaccount(case.vault,'123456')
    if manual=='UNKNOWN':
        with case.vault.session() as db:db.delete(db.get(GrvtMainnetStopOrigin,'123456'));db.commit()
    close_remote_group(case,parent)
    for _ in range(2):
        audit_mainnet_bracket(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.clock[0]+=6_000_000_000
    reconcile_mainnet_bracket(case.vault,parent,now_ns=case.clock[0]);settle_bot_selection(case.vault,grant['id'],now_ns=case.clock[0])
    with case.vault.session() as db:revision=_control(db,'123456').revision
    with pytest.raises(GrvtMainnetError):release_bot_stop(case.vault,grant['id'],parent,expected_revision=revision,require_permission=lambda:None,now_ns=case.clock[0])
    with case.vault.session() as db:
        control=_control(db,'123456');assert control.stopped and control.revision==revision
        row=db.get(GrvtMainnetStopOrigin,'123456')
        assert row is None if manual=='UNKNOWN' else __import__('json').loads(row.payload_json)['kind']=='MANUAL'
    case.vault.backup(tmp_path/'manual-stop-remains.db');case.vault.lock()


def test_v53_migration_preserves_grants_without_inventing_stop_origin_or_authority(tmp_path):
    from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
    from app.live_execution.grvt_mainnet_bot_service import GrvtMainnetBotService
    from app.live_execution.grvt_mainnet_journal import stop_mainnet_subaccount
    case,origin,source,emit,grant,policy=grant_case(tmp_path)
    stop_mainnet_subaccount(case.vault,'123456')
    with case.vault.session() as db:
        for table in ('grvt_mainnet_bot_exits','grvt_mainnet_stop_origins'):db.connection().exec_driver_sql('DROP TABLE '+table)
        db.connection().exec_driver_sql('PRAGMA user_version=53');db.commit()
    case.vault.lock();case.vault.unlock(PASSWORD)
    assert case.vault.last_migration_backup.is_file() and read_bot_grant(case.vault,grant['id'])==grant
    cold=GrvtMainnetBotService(case.vault).snapshot(grant['id'])
    assert cold['status']=='INTERRUPTED' and not cold['active'] and cold['authorization'] is False
    with case.vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
        assert db.connection().exec_driver_sql('SELECT count(*) FROM grvt_mainnet_stop_origins').scalar_one()==0
        assert db.connection().exec_driver_sql('SELECT count(*) FROM grvt_mainnet_bot_exits').scalar_one()==0
    case.vault.backup(tmp_path/'v54-migrated.db');case.vault.lock()
