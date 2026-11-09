"""End-to-end encrypted supervision with actual source and worker threads."""
from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
from app.live_execution.grvt_mainnet_bot_service import GrvtMainnetBotService
from tests.grvt_perp_coordinator_fixture import OwnedPerpCoordinator
from tests.test_grvt_mainnet_bot_service import wait_for
from tests.test_grvt_mainnet_submit import PASSWORD

import pytest
from threading import Event


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_independent_future_signal_actual_entry_position_bidask_exit_and_financial_receipt(tmp_path,monkeypatch,direction,mode):
    owned=OwnedPerpCoordinator(tmp_path,monkeypatch,direction=direction,mode=mode)
    try:
        started=owned.start()
        done=wait_for(lambda:(value if not (value:=owned.service.snapshot(started['id']))['active'] else None),seconds=45)
        assert done['status']=='ENTRY_LIMIT',done
        assert done['completed_entries']==1 and done['financial_complete']
        assert len(owned.case.sent)==1 and len(owned.case.settings['reduction_posts'])==1
        # The software trigger is 151/49. Actual exit filled at 100, so the
        # receipt contains only the real .02 entry and .01 exit fees.
        assert done['net_realized_pnl_usdt']=='-0.03'
        assert owned.case.settings['positions']==[]
        assert done['selections'][0]['plan']['source']['terms']['profile']['margin_mode']==mode
        job=owned.service._jobs[started['id']]
        assert job.key is None and not job.api_key and not job.signer
        assert job.perp_worker.join(5)
        owned.case.vault.backup(tmp_path/'supervised-cycle.db')
        owned.case.vault.lock();owned.case.vault.unlock(PASSWORD)
        cold=GrvtMainnetBotService(owned.case.vault).snapshot(started['id'])
        assert cold['status']=='ENTRY_LIMIT' and not cold['active'] and not cold['authorization']
        assert read_bot_grant(owned.case.vault,started['id'])['supervision']['entry_state']=='NONE'
    finally:owned.close()


def test_source_failure_closes_only_owned_residue_records_halt_and_never_rearms(tmp_path,monkeypatch):
    owned=OwnedPerpCoordinator(tmp_path,monkeypatch,policy_changes={'max_entries':2})
    owned.close_on_watch=False;owned.worker_fail_on_watch=True
    try:
        started=owned.start()
        done=wait_for(lambda:(value if not (value:=owned.service.snapshot(started['id']))['active'] else None),seconds=45)
        assert done['status']=='REVIEW',done
        assert done['supervision']['halt_new_entries'] and done['supervision']['halt_reason']=='SOURCE_ERROR'
        assert done['completed_entries']==1 and done['net_realized_pnl_usdt']=='-0.03'
        assert len(owned.case.sent)==1 and len(owned.case.settings['reduction_posts'])==1
    finally:owned.close()


def test_stop_during_public_http_revokes_before_response_without_a_market_write_or_send(tmp_path,monkeypatch):
    from sqlalchemy import select
    from app.portfolio.models import GrvtMainnetBotEvent
    owned=OwnedPerpCoordinator(tmp_path,monkeypatch)
    entered=Event();release=Event();original=owned.owned.adapter.get_ticker;calls=0
    def ticker(symbol):
        nonlocal calls
        calls+=1
        if calls==2:
            entered.set()
            assert release.wait(15)
        return original(symbol)
    owned.owned.adapter.get_ticker=ticker
    try:
        started=owned.start()
        assert entered.wait(10)
        owned.service.stop_subaccount('123456')
        job=owned.service._jobs[started['id']]
        assert job.stop.is_set() and job.perp_worker.candidate() is None
        assert not job.perp_worker.join(0)  # HTTP has not returned yet.
        release.set()
        done=wait_for(lambda:(value if not (value:=owned.service.snapshot(started['id']))['active'] else None))
        assert done['status']=='STOPPED' and not owned.case.sent
        assert job.perp_worker.join(5) and job.key is None
        with owned.case.vault.session() as db:
            events=list(db.scalars(select(GrvtMainnetBotEvent).where(GrvtMainnetBotEvent.run_id==started['id'])))
        import json
        assert [json.loads(row.payload_json)['type'] for row in events]==['FINISH']
    finally:
        release.set();owned.close()


def test_manager_thread_start_failure_revokes_signer_and_finishes_grant_without_an_entry(tmp_path,monkeypatch):
    import app.live_execution.grvt_perp_coordinator as module
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    owned=OwnedPerpCoordinator(tmp_path,monkeypatch)
    class FailedThread:
        def __init__(self,**kwargs):pass
        def start(self):raise RuntimeError('Owned manager thread allocation failure')
    monkeypatch.setattr(module,'Thread',FailedThread)
    try:
        with pytest.raises(GrvtMainnetError,match='No se pudo activar'):owned.start()
        assert len(owned.service._jobs)==1
        job=next(iter(owned.service._jobs.values()))
        assert not job.active and job.stop.is_set() and job.key is None and not job.api_key and not job.signer
        assert job.perp_worker.join(5)
        saved=read_bot_grant(owned.case.vault,job.saved['id'])
        assert saved['terminal']['status']=='STOPPED' and not saved['selections']
        assert not owned.service.active_sub('123456') and not owned.case.sent
    finally:owned.close()


def test_lost_entry_and_reduction_responses_use_original_ids_without_duplicate_send(tmp_path,monkeypatch):
    owned=OwnedPerpCoordinator(tmp_path,monkeypatch)
    owned.case.settings.update(lose=True,reduction_lost=True)
    try:
        started=owned.start()
        done=wait_for(lambda:(value if not (value:=owned.service.snapshot(started['id']))['active'] else None),seconds=45)
        assert done['status']=='ENTRY_LIMIT' and done['net_realized_pnl_usdt']=='-0.03',done
        assert len(owned.case.sent)==1 and len(owned.case.settings['reduction_posts'])==1
    finally:owned.close()


def test_two_cycles_share_budget_and_rearm_only_after_actual_settlement(tmp_path,monkeypatch):
    owned=OwnedPerpCoordinator(tmp_path,monkeypatch,policy_changes={'max_entries':2})
    try:
        started=owned.start()
        done=wait_for(lambda:(value if not (value:=owned.service.snapshot(started['id']))['active'] else None),seconds=60)
        assert done['status']=='ENTRY_LIMIT' and done['completed_entries']==2,done
        first,second=done['selections']
        assert int(second['signal']['source_close_ns'])>first['settled_at_ns']
        assert second['selected_at_ns']>=first['settled_at_ns']
        assert done['entry_budget_reserved']=='2000' and done['net_realized_pnl_usdt']=='-0.06'
        assert len(owned.case.sent)==2 and len(owned.case.settings['reduction_posts'])==2
    finally:owned.close()


@pytest.mark.parametrize('fault,reason',[('configuration','LEVERAGE_MISMATCH'),('equity','LOSS_LIMIT')])
def test_live_position_risk_closes_and_disables_following_entries(tmp_path,monkeypatch,fault,reason):
    owned=OwnedPerpCoordinator(tmp_path,monkeypatch,policy_changes={'max_entries':2})
    owned.close_on_watch=False
    def change():
        if fault=='configuration':owned.case.config['leverage']='4'
        else:
            from decimal import Decimal
            owned.case.settings['account_equity']='-1'
            owned.case.settings['unrealized_pnl']=str(-1-Decimal(owned.case.settings['account_balance']))
    owned.on_watch=change
    try:
        started=owned.start()
        done=wait_for(lambda:(value if not (value:=owned.service.snapshot(started['id']))['active'] else None),seconds=45)
        assert done['status']=='REVIEW' and done['completed_entries']==1,done
        assert done['supervision']['halt_reason']==reason and done['supervision']['halt_new_entries']
        assert done['net_realized_pnl_usdt']=='-0.03'
        assert len(owned.case.sent)==1 and len(owned.case.settings['reduction_posts'])==1
    finally:owned.close()
