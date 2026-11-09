"""New grant/source, causal journal, typed preflight and shared protected send."""
from decimal import Decimal
import json

import pytest

from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_perp_bot_grant import SCHEMA
from tests.grvt_perp_bot_fixture import perp_bot_case,settle_owned_native
from tests.test_grvt_mainnet_submit import PASSWORD


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_replayed_origin_independent_signals_and_real_budget_select_once_before_protected_send(tmp_path,monkeypatch,direction,mode):
    owned=perp_bot_case(tmp_path,monkeypatch,direction=direction,mode=mode)
    case=owned.case
    try:
        assert owned.grant['schema']==SCHEMA and owned.grant['authorization'] is False
        assert not owned.grant['selections'] and not case.sent
        assert 'signal_seed' not in owned.grant and owned.grant['signal_checkpoint']['evaluated_bars']==0
        owned.emit()
        saved=read_bot_grant(case.vault,owned.grant['id'])
        assert saved['signal_checkpoint']['evaluated_bars']==1 and saved['latest_signal']['entry_matched']
        result=owned.send(saved)
        assert result['claim']['schema']=='orbis.grvt.mainnet.bracket.claim.v3'
        assert result['claim']['source']=='PERP_BOT_NATIVE_PROTECTED_ENTRY'
        assert len(case.sent)==1 and result['claim']['draft']['direction']==direction
        plan=result['claim']['preflight']['plan']
        assert plan['source']['terms']['profile']['margin_mode']==mode
        assert plan['result']['account_equity']=='100' and plan['result']['entry_cash_budget']=='25'
        saved=read_bot_grant(case.vault,owned.grant['id'])
        assert saved['selections'][0]['plan']==plan
        before=len(case.calls)
        with pytest.raises(GrvtMainnetError):owned.send(saved)
        assert len(case.calls)==before and len(case.sent)==1
        case.vault.backup(tmp_path/'owned-perp-bot-backup.db')
        assert case.vault._grvt_perp_replay_cache
        case.vault.lock()
        assert not case.vault._grvt_perp_replay_cache
        case.vault.unlock(PASSWORD)
        assert read_bot_grant(case.vault,saved['id'])==saved
        assert not read_bot_grant(case.vault,saved['id'])['authorization']
    finally:case.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_actual_fills_funding_and_fee_receipt_settle_the_same_grant_then_finish(tmp_path,monkeypatch,direction):
    from app.live_execution.grvt_mainnet_bot_entry import finish_bot_grant
    owned=perp_bot_case(tmp_path,monkeypatch,direction=direction,policy_changes={'max_entries':1})
    case=owned.case
    try:
        owned.emit()
        sent=owned.send(read_bot_grant(case.vault,owned.grant['id']))
        receipt,saved,net=settle_owned_native(owned,sent['parent_client_order_id'],funding='-0.125')
        assert Decimal(receipt['result']['net_realized_pnl_usdt'])==net
        assert saved['selections'][0]['settlement']['net_realized_pnl_usdt']==str(net)
        assert saved['financial_outcomes'][0]['bot_recorded'] is True
        saved=finish_bot_grant(case.vault,saved['id'],status='ENTRY_LIMIT',now_ns=case.clock[0])
        assert saved['terminal']=={'status':'ENTRY_LIMIT'} and saved['authorization'] is False
        case.vault.lock();case.vault.unlock(PASSWORD)
        assert read_bot_grant(case.vault,saved['id'])==saved
        assert len(case.sent)==1
    finally:case.vault.lock()


@pytest.mark.parametrize('fault',['bad_fill','unlinked_funding'])
def test_fill_outside_signed_limit_or_unlinked_funding_keeps_the_grant_unsettled(tmp_path,monkeypatch,fault):
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        owned.emit()
        sent=owned.send(read_bot_grant(case.vault,owned.grant['id']))
        with pytest.raises(GrvtMainnetError,match='Conciliación MAINNET no verificable'):
            settle_owned_native(owned,sent['parent_client_order_id'],**{fault:True})
        saved=read_bot_grant(case.vault,owned.grant['id'])
        assert saved['selections'][0]['settlement'] is None and not saved['financial_outcomes']
        assert len(case.sent)==1
    finally:case.vault.lock()


@pytest.mark.parametrize('schema,source',[
    ('orbis.grvt.mainnet.bracket.claim.v2','PERP_PROFILE_NATIVE_PROTECTED_ENTRY'),
    ('orbis.grvt.mainnet.bracket.claim.v1','BOT_NATIVE_PROTECTED_ENTRY')])
def test_rehashed_claim_cannot_downgrade_new_bot_to_manual_or_legacy(tmp_path,monkeypatch,schema,source):
    from app.portfolio.models import GrvtMainnetBracket
    from app.live_execution.grvt_mainnet_journal import encode,read_mainnet_bracket
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        owned.emit()
        sent=owned.send(read_bot_grant(case.vault,owned.grant['id']))
        parent=sent['parent_client_order_id']
        with case.vault.session() as db:
            row=db.get(GrvtMainnetBracket,parent)
            payload=json.loads(row.payload_json)
            payload.update(schema=schema,source=source)
            row.payload_json,row.payload_digest=encode(payload)
            # No acknowledgement dependency is needed to reject the relabel.
            row.ack_json=row.ack_digest=None
            row.status='POST_CLAIMED'
            db.commit()
        with pytest.raises(GrvtMainnetError):read_mainnet_bracket(case.vault,parent)
        assert len(case.sent)==1
    finally:case.vault.lock()


def test_lost_post_preserves_v3_claim_and_cold_reopen_never_retransmits(tmp_path,monkeypatch):
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        owned.emit()
        saved=read_bot_grant(case.vault,owned.grant['id'])
        case.settings['lose']=True
        with pytest.raises(GrvtMainnetError):owned.send(saved)
        selected=read_bot_grant(case.vault,saved['id'])['selections'][0]
        parent=selected['parent_client_order_id']
        assert read_mainnet_bracket(case.vault,parent)['status']=='POST_CLAIMED'
        case.vault.lock();case.vault.unlock(PASSWORD)
        before=len(case.calls)
        with pytest.raises(GrvtMainnetError):owned.send(read_bot_grant(case.vault,saved['id']))
        assert len(case.calls)==before and len(case.sent)==1
        observed=observe_mainnet_bracket(case.vault,parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        assert observed['state']['entry_fully_filled'] and len(case.sent)==1
    finally:case.vault.lock()


@pytest.mark.parametrize('after',['selection','claim'])
def test_ram_revocation_after_durable_step_prevents_post_and_burns_selection(tmp_path,monkeypatch,after):
    from app.live_execution.grvt_perp_bot_grant import PerpBotSelectionRequest,_signal_selection
    from app.live_execution.grvt_perp_submit import submit_perp_entry_once
    import app.live_execution.grvt_perp_submit as submit
    import app.live_execution.grvt_mainnet_submit as shared
    from tests.test_grvt_mainnet_submit import arguments
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    allowed=[True]
    try:
        owned.emit()
        saved=read_bot_grant(case.vault,owned.grant['id'])
        _,parent,_=_signal_selection(saved['source'],saved['id'],saved['latest_signal'])
        module,name=(submit,'select_perp_entry') if after=='selection' else (shared,'claim_mainnet_bracket')
        original=getattr(module,name)
        def revoked(*args,**kwargs):
            result=original(*args,**kwargs)
            allowed[0]=False
            return result
        monkeypatch.setattr(module,name,revoked)
        def permission():
            if not allowed[0]:raise GrvtMainnetError('Owned permission revoked')
        args=arguments(case)
        args.update(confirmation='ENVIAR_ENTRADA_PERPETUA_GRVT_MAINNET',require_permission=permission)
        with pytest.raises(GrvtMainnetError):
            submit_perp_entry_once(case.vault,owned.terms,parent_client_order_id=parent,
                bot_selection=PerpBotSelectionRequest(saved['id'],saved['latest_signal']['sha256']),**args)
        assert len(read_bot_grant(case.vault,saved['id'])['selections'])==1 and not case.sent
        before=len(case.calls)
        with pytest.raises(GrvtMainnetError):owned.send(read_bot_grant(case.vault,saved['id']))
        assert len(case.calls)==before and not case.sent
    finally:case.vault.lock()


def test_waiting_perpetual_grant_excludes_manual_and_second_grants(tmp_path,monkeypatch):
    from app.live_execution.grvt_perp_bot_grant import begin_perp_bot_grant
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from tests.test_grvt_mainnet_submit import arguments
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        before=len(case.calls)
        with pytest.raises(GrvtMainnetError):
            submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=str(2**63+19001),**arguments(case))
        with pytest.raises(GrvtMainnetError):
            begin_perp_bot_grant(case.vault,owned.origin,owned.seed,owned.terms,owned.policy,now_ns=case.clock[0])
        with pytest.raises(GrvtMainnetError,match='origen LAB/PAPER verificado'):
            begin_perp_bot_grant(case.vault,owned.origin.source().payload(),owned.seed,owned.terms,owned.policy,now_ns=case.clock[0])
        assert len(case.calls)==before and not case.sent
    finally:case.vault.lock()


def test_stale_signal_stops_before_private_preflight(tmp_path,monkeypatch):
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        owned.emit()
        saved=read_bot_grant(case.vault,owned.grant['id'])
        case.clock[0]+=16*10**9
        before=len(case.calls)
        with pytest.raises(GrvtMainnetError,match='caducada'):owned.send(saved)
        assert len(case.calls)==before and not case.sent
    finally:case.vault.lock()


@pytest.mark.parametrize('edit',['decision','checkpoint','input','delete','relabel'])
def test_warm_cache_rechecks_actual_prefix_bytes_and_rejects_rehashed_corruption(tmp_path,monkeypatch,edit):
    from app.portfolio.models import GrvtMainnetBotEvent,GrvtMainnetBotRun
    from app.live_execution.grvt_mainnet_journal import encode
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        owned.emit()
        saved=read_bot_grant(case.vault,owned.grant['id'])
        assert case.vault._grvt_perp_replay_cache
        with case.vault.session() as db:
            row=db.get(GrvtMainnetBotRun,saved['id'])
            if edit=='relabel':
                value=json.loads(row.payload_json)
                value['schema']='orbis.grvt.mainnet.bot.grant.v3'
                row.payload_json,row.payload_digest=encode(value)
            else:
                event=db.get(GrvtMainnetBotEvent,(saved['id'],row.event_count))
                if edit=='delete':db.delete(event)
                else:
                    value=json.loads(event.payload_json)
                    if edit=='decision':value['details']['decisions'][0]['entry_matched']=False
                    elif edit=='checkpoint':value['details']['checkpoint']['matched_entries']=99
                    else:value['details']['input']['closed_candles'][0]['v']='123'
                    event.payload_json,event.payload_digest=encode(value)
                    row.head_digest=event.payload_digest
            db.commit()
        with pytest.raises(GrvtMainnetError):read_bot_grant(case.vault,saved['id'])
        assert not case.sent
    finally:case.vault.lock()


def test_archive_rollback_does_not_publish_or_trust_an_uncommitted_cached_prefix(tmp_path,monkeypatch):
    from app.live_execution.grvt_perp_bot_grant import append_perp_market
    from app.live_execution.grvt_perp_signals import record_signal_input
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        state=owned.seed.start()
        owned.clock['now']+=10*10**9
        _,record=record_signal_input(state,owned.source.poll(state))
        calls=[]
        def revoked():
            calls.append(True)
            if len(calls)==3:raise GrvtMainnetError('Owned precommit revocation')
        with pytest.raises(GrvtMainnetError):
            append_perp_market(case.vault,owned.grant['id'],record,require_permission=revoked,clock_ns=lambda:owned.clock['now'])
        saved=read_bot_grant(case.vault,owned.grant['id'])
        assert saved['signal_checkpoint']['sequence']==0 and not saved['latest_signal']
        assert not case.sent
    finally:case.vault.lock()


def test_hot_prefix_skips_recalculation_but_full_replay_matches_it(tmp_path,monkeypatch):
    import app.live_execution.grvt_perp_bot_grant as grant
    from app.portfolio.models import GrvtMainnetBotRun
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        owned.emit()
        warm=read_bot_grant(case.vault,owned.grant['id'])
        original=grant.verify_signal_record
        def forbidden(*args,**kwargs):raise AssertionError('Historical indicators recalculated in hot prefix')
        monkeypatch.setattr(grant,'verify_signal_record',forbidden)
        assert read_bot_grant(case.vault,warm['id'])==warm
        monkeypatch.setattr(grant,'verify_signal_record',original)
        with case.vault.session() as db:
            cold=grant.read_perp_bot_grant(db,db.get(GrvtMainnetBotRun,warm['id']),verify_full=True)
        assert cold==warm
    finally:case.vault.lock()


def test_documented_funding_execution_prefix_reconciles_the_new_claim(tmp_path,monkeypatch):
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        owned.emit()
        sent=owned.send(read_bot_grant(case.vault,owned.grant['id']))
        receipt,saved,net=settle_owned_native(owned,sent['parent_client_order_id'],numeric_group=True)
        assert Decimal(receipt['result']['funding_paid_usdt'])==Decimal('.125')
        assert Decimal(receipt['result']['net_realized_pnl_usdt'])==net
        assert saved['selections'][0]['settlement'] is not None
    finally:case.vault.lock()


def test_cold_subprocess_replays_encrypted_grant_without_network_or_permission(tmp_path,monkeypatch):
    import subprocess
    import sys
    owned=perp_bot_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        owned.emit()
        owned.send(read_bot_grant(case.vault,owned.grant['id']))
        case.vault.lock()
        code='''
import sys,json,socket
from pathlib import Path
def forbidden(*args,**kwargs):raise AssertionError('Cold replay attempted network')
socket.socket.connect=forbidden
from app.portfolio.vault import RealVault
from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
args=json.load(sys.stdin)
vault=RealVault(Path(args['path']));vault.unlock(args['password'])
try:
 value=read_bot_grant(vault,args['run'])
 print(json.dumps({'schema':value['schema'],'authorization':value['authorization'],
  'selections':len(value['selections']),'bars':value['signal_checkpoint']['evaluated_bars']}))
finally:vault.lock()
'''
        completed=subprocess.run([sys.executable,'-c',code],
            input=json.dumps({'path':str(case.vault.path),'password':PASSWORD,'run':owned.grant['id']}),
            capture_output=True,text=True,timeout=30)
        assert completed.returncode==0,completed.stderr
        assert json.loads(completed.stdout)=={'schema':SCHEMA,'authorization':False,'selections':1,'bars':1}
    finally:case.vault.lock()


def test_expiry_can_be_recorded_after_the_deadline_without_restoring_permission(tmp_path,monkeypatch):
    from app.live_execution.grvt_mainnet_bot_entry import finish_bot_grant
    owned=perp_bot_case(tmp_path,monkeypatch)
    try:
        saved=finish_bot_grant(owned.case.vault,owned.grant['id'],status='EXPIRED',now_ns=owned.grant['deadline_ns']+1)
        assert saved['terminal']=={'status':'EXPIRED'} and saved['authorization'] is False
        assert not owned.case.sent
    finally:owned.case.vault.lock()


@pytest.mark.parametrize('identifier,digest',[('invalid','a'*64),('11111111-1111-4111-8111-111111111111','bad'),
    (None,'a'*64),('11111111-1111-4111-8111-111111111111',True)])
def test_selection_identity_is_strict_without_storage_or_http(identifier,digest):
    from app.live_execution.grvt_perp_bot_grant import PerpBotSelectionRequest
    with pytest.raises(GrvtMainnetError):PerpBotSelectionRequest(identifier,digest)


@pytest.mark.parametrize('change',['lab_lock','paper_lock','changed_lab'])
def test_origin_capability_expires_with_its_original_stores(tmp_path,monkeypatch,change):
    from app.derivatives.paper_access import paper_perp_access
    from app.lab.access import lab_access
    owned=perp_bot_case(tmp_path,monkeypatch)
    try:
        if change=='lab_lock':lab_access.lock(owned.lab.path)
        elif change=='paper_lock':paper_perp_access.lock(owned.paper.path)
        else:
            with owned.lab.connect() as db:
                db.execute("UPDATE strategy_versions SET definition_json='{}'")
        with pytest.raises(ValueError):
            with owned.origin.hold_current():pass
        assert not owned.case.sent
    finally:owned.case.vault.lock()


def test_rehashed_seed_cannot_replace_the_original_lab_price_prefix(tmp_path,monkeypatch):
    from app.live_execution.grvt_perp_signals import PerpSignalSeed
    from app.live_execution.grvt_perp_bot_grant import begin_perp_bot_grant
    owned=perp_bot_case(tmp_path,monkeypatch)
    try:
        payload=owned.seed.payload()
        payload['history'][0]['v']='999'
        changed=PerpSignalSeed.model_validate(payload)
        with pytest.raises(GrvtMainnetError,match='prefijo LAB original'):
            begin_perp_bot_grant(owned.case.vault,owned.origin,changed,owned.terms,owned.policy,now_ns=owned.clock['now'])
        assert not owned.case.sent
    finally:owned.case.vault.lock()


def test_market_capacity_reserves_space_for_a_terminal_record(tmp_path,monkeypatch):
    import app.live_execution.grvt_perp_bot_grant as grant
    from app.live_execution.grvt_mainnet_bot_entry import finish_bot_grant
    owned=perp_bot_case(tmp_path,monkeypatch)
    try:
        monkeypatch.setattr(grant,'MAX_MARKET_EVENTS',1)
        with pytest.raises(GrvtMainnetError,match='evento de mercado'):owned.emit()
        saved=read_bot_grant(owned.case.vault,owned.grant['id'])
        assert saved['signal_checkpoint']['sequence']==1
        saved=finish_bot_grant(owned.case.vault,saved['id'],status='STOPPED',now_ns=owned.clock['now'])
        assert saved['terminal']=={'status':'STOPPED'} and not owned.case.sent
    finally:owned.case.vault.lock()


def test_legacy_bot_cannot_take_a_subaccount_reserved_by_a_perpetual_grant(tmp_path,monkeypatch):
    from app.live_execution.grvt_mainnet_bot_entry import begin_bot_grant
    from tests.grvt_testnet_ui_fixture import GrvtTestnetScenario
    owned=perp_bot_case(tmp_path,monkeypatch)
    try:
        directory=tmp_path/'legacy-source'
        directory.mkdir()
        legacy=GrvtTestnetScenario(directory)
        source=legacy.seed('LONG',defer_open=True)
        with pytest.raises(GrvtMainnetError):
            begin_bot_grant(owned.case.vault,legacy.paper,legacy.lab,source['session_id'],owned.policy,now_ns=owned.clock['now'])
        assert not owned.case.sent
    finally:owned.case.vault.lock()
