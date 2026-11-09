"""Automated LIMIT protection against an owned GRVT HTTP simulation."""
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.lab.perp_dataset import TradeBar
from app.live_execution import grvt_perp_limit_execution as execution
from app.live_execution import grvt_perp_limit_supervisor as supervisor
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_journal import encode, stop_mainnet_subaccount
from app.live_execution.grvt_perp_limit_journal import read_limit_attempt
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import replay_signal_records
from app.portfolio.models import GrvtMainnetLimitAttempt, GrvtMainnetLimitEvent
from tests.perp_observed_source_fixture import OwnedPublicAdapter
from tests.test_grvt_perp_limit_entry import CID, NS, fill
from tests.test_grvt_perp_limit_execution import SUB, VENUE_ORDER_ID, cold, setup
from tests.test_grvt_perp_limit_exit import FIRST_EXIT, SECOND_EXIT, controlled_exit, position
from tests.test_grvt_testnet_signer import KEY, SIGNER


def armed_case(tmp_path, monkeypatch, *, max_reductions=2, lose_cancel=False):
    case,calls,session,send = setup(tmp_path,monkeypatch,lose_cancel=lose_cancel)
    execution.submit_limit_entry_once(case.vault,**send)
    case.clock[0] += NS
    order = case.settings['orders'][CID]
    order['state'].update(traded_size=['0.3'],update_time=str(case.clock[0]),
                          book_size=[str(Decimal(order['legs'][0]['size'])-Decimal('0.3'))])
    case.settings['histories']['fill_history'] = [
        fill(size='0.3',price='99.9',order_id=VENUE_ORDER_ID,event_time=str(case.clock[0]))]
    feed = replay_signal_records(send['seed'],send['records']).state
    fixture = SimpleNamespace(history=[TradeBar.model_validate(bar.json()) for bar in feed.history],
                              funding=[],quote=send['seed'].quote)
    adapter = OwnedPublicAdapter(fixture,{'now':case.clock[0]})
    original = adapter.get_ticker

    def ticker(symbol):
        adapter.clock['now'] = case.clock[0]
        return original(symbol)

    adapter.get_ticker = ticker
    adapter.quote_override.update(bid='99.99',ask='100.01')
    source = GrvtPerpSignalSource(adapter=adapter,clock_ns=session['clock_ns'])
    session,posts = controlled_exit(case,session,'LONG')
    ids = iter((int(FIRST_EXIT)-2**63,int(SECOND_EXIT)-2**63))
    monkeypatch.setattr(supervisor,'secrets',SimpleNamespace(randbits=lambda _:next(ids)))
    guard = {'confirmation':'SUPERVISAR_ORDEN_LIMIT_GRVT_MAINNET','private_key':KEY,
             'expected_signer':SIGNER,'deadline_ns':case.clock[0]+120*NS,'max_reductions':max_reductions}
    args = {**guard,**session,'source':source,'public_post':case.public}
    controller = supervisor.LimitSupervisor(case.vault,CID,**args)
    return case,controller,adapter,calls,posts,args


def prove_position(case):
    case.settings['histories']['funding_payment_history'] = [
        {'sub_account_id':SUB,'event_time':str(case.clock[0]),'tx_id':'during-cancel',
         'instrument':'BTC_USDT_Perp','currency':'USDT','amount':'0.005'}]
    case.settings.update(account_balance='99.995',account_equity='99.995',positions=[position('0.5')])


@pytest.mark.parametrize('max_reductions',[1,2])
def test_supervisor_cancels_partial_then_triggers_and_reconciles_automatic_exit(tmp_path, monkeypatch, max_reductions):
    case,controller,adapter,calls,posts,_ = armed_case(tmp_path,monkeypatch,max_reductions=max_reductions,
                                                      lose_cancel=True)
    initial = controller.step()
    assert initial['status'] == 'WAIT_TERMINAL' and initial['decision']['reason'] == 'PARTIAL_ENTRY'
    assert initial['decision']['entry_quantity'] == '0.3' and not posts and len(calls) == 2
    prove_position(case)
    case.clock[0] += NS
    hold = controller.step()
    assert hold['status'] == 'HOLD' and hold['decision']['entry_quantity'] == '0.5'
    assert hold['decision']['exit']['entry_notional'] == '49.95'
    case.clock[0] += NS
    adapter.quote_override.update(bid='49',ask='49.01',mark_price='49')
    trigger = controller.step()
    assert trigger['status'] == 'VERIFYING_EXIT' and trigger['decision']['close_reason'] == 'STOP_OR_TRAILING'
    assert trigger['decision']['exit']['trigger_price'] == '49' and not posts
    adapter.quote_override.update(bid='99.99',ask='100.01',mark_price='100')
    case.clock[0] += 4*NS
    assert controller.step()['status'] == 'VERIFYING_EXIT'
    assert len(posts) == 1
    saved = read_limit_attempt(case.vault,CID)['events']
    assert saved['reductions'][0]['ack'] is None
    assert saved['reductions'][0]['claim']['supervision_head'] == saved['supervision_head']
    case.clock[0] += NS
    assert controller.step()['status'] == 'VERIFYING_EXIT' and len(posts) == 1
    case.clock[0] += 5*NS
    if max_reductions == 1:
        assert controller.step()['status'] == 'REVIEW'
        assert controller._key is None and len(posts) == 1
        recovered = read_limit_attempt(case.vault,CID)
        assert recovered['slot_reserved']
        assert recovered['events']['exit_audits'][-1]['result']['remaining_size'] == '0.3'
        case.vault.lock()
        return
    assert controller.step()['status'] == 'VERIFYING_EXIT' and len(posts) == 2
    case.clock[0] += NS
    assert controller.step()['status'] == 'VERIFYING_EXIT'
    case.clock[0] += 5*NS
    final = controller.step()
    assert final['status'] == 'COMPLETE' and final['decision']['close_reason'] == 'STOP_OR_TRAILING'
    assert controller._key is None
    case.vault.lock()
    reopened = cold(case.vault.path)
    try:
        recovered = read_limit_attempt(reopened,CID)
        assert recovered['events']['position_settled'] and not recovered['authorization']
        assert recovered['events']['exit_audits'][-1]['result']['net_change_usdt'] == '0.025'
        assert recovered['events']['supervision']['exit']['trigger_price'] == '49'
        assert len(calls) == len(posts) == 2
    finally:
        reopened.lock()


def test_stop_revokes_supervision_even_with_confirmed_close(tmp_path, monkeypatch):
    case,controller,adapter,calls,posts,args = armed_case(tmp_path,monkeypatch)
    controller.step()
    prove_position(case)
    case.clock[0] += NS
    adapter.quote_override.update(bid='49',ask='49.01',mark_price='49')
    assert controller.step()['decision']['close_reason'] == 'STOP_OR_TRAILING'
    case.clock[0] += 5*NS
    stop_mainnet_subaccount(case.vault,SUB)
    with pytest.raises(GrvtMainnetError,match='revocada'):
        controller.step()
    with pytest.raises(GrvtMainnetError,match='no admite'):
        supervisor.LimitSupervisor(case.vault,CID,**args)
    assert read_limit_attempt(case.vault,CID)['slot_reserved']
    assert controller._key is None and not posts and len(calls) == 2
    case.vault.lock()


def test_watchdog_records_stale_market_and_replays_original(tmp_path, monkeypatch):
    case,controller,adapter,_,posts,_ = armed_case(tmp_path,monkeypatch)
    controller.step()
    prove_position(case)
    adapter.fail = 'quote'
    case.clock[0] += 6*NS
    held = controller.step()
    assert held['decision']['close_reason'] == 'DATA_STALE' and not posts
    with case.vault.session() as db:
        attempt = db.get(GrvtMainnetLimitAttempt,CID)
        last = db.get(GrvtMainnetLimitEvent,(CID,attempt.event_count-1))
        event = json.loads(last.payload_json)
        assert event['kind'] == 'WATCH' and event['detail']['record'] is None
        event['detail']['checkpoint']['close_reason'] = 'TAKE_PROFIT'
        last.payload_json,last.payload_digest = encode(event)
        audit = db.get(GrvtMainnetLimitEvent,(CID,attempt.event_count))
        audit_event = json.loads(audit.payload_json)
        audit_event['previous'] = last.payload_digest
        audit.payload_json,audit.payload_digest = encode(audit_event)
        attempt.event_head = audit.payload_digest
        db.commit()
    with pytest.raises(GrvtMainnetError,match='no verificable'):
        read_limit_attempt(case.vault,CID)
    controller.stop()
    case.vault.lock()
