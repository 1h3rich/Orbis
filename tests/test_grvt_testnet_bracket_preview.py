"""Revisión manual sin firma ni reserva, sobre señales LAB/PAPER verificadas."""

from decimal import Decimal
import time

import pytest

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import read_bracket
from tests.test_grvt_testnet_bracket_submit import live_group


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_preview_replays_the_signal_and_compiles_three_unsigned_orders_without_reserving(tmp_path,direction):
    from app.live_execution.grvt_testnet_bracket_preview import preview_bracket_entry
    venue,source,vault,_,cap,_,client,public,recorded=live_group(tmp_path,direction)
    prepared=preview_bracket_entry(venue.paper,venue.lab,vault,client,sub=cap.sub_account_id,
        session_id=source['session_id'],action_request_id=source['action_request_id'],
        size=Decimal('0.5'),limit_price=Decimal('100'),entry_cap=Decimal('100'),exit_cap=Decimal('100'),
        stop_loss=Decimal('95' if direction=='LONG' else '105'),
        take_profit=Decimal('110' if direction=='LONG' else '90'),ttl_minutes=60,
        max_spread_bps=25,max_slippage_bps=25,public_post=public,clock_ns=time.time_ns)
    assert prepared['network']=='TESTNET' and prepared['mode']=='MANUAL_PROTECTED_ENTRY'
    assert prepared['source']['client_order_id']==source['client_order_id']
    assert [leg['role'] for leg in prepared['legs']]==['ENTRY','STOP_LOSS','TAKE_PROFIT']
    assert prepared['entry_side']==('BUY' if direction=='LONG' else 'SELL')
    assert isinstance(prepared['entry_expiration_ns'],str)
    assert recorded['sends']==[]
    with pytest.raises(GrvtTestnetBracketError): read_bracket(vault,source['client_order_id'])
    assert 'signature' not in str(prepared) and 'fake-testnet-key' not in str(prepared)
    vault.lock()


def test_preview_rejects_a_corrupted_replay_without_reading_exchange_data(tmp_path):
    from app.live_execution.grvt_testnet_bracket_preview import preview_bracket_entry
    from app.live_execution.grvt_testnet_signal import GrvtTestnetSignalError
    venue,source,vault,_,cap,_,client,public,recorded=live_group(tmp_path)
    prior_calls=(list(recorded['public']),list(recorded['private']))
    with venue.paper.connect() as db:
        db.execute("UPDATE paper_signal_observations SET evidence_json='{}' WHERE session_id=?",(source['session_id'],))
    with pytest.raises(GrvtTestnetSignalError):
        preview_bracket_entry(venue.paper,venue.lab,vault,client,sub=cap.sub_account_id,
            session_id=source['session_id'],action_request_id=source['action_request_id'],
            size=Decimal('0.5'),limit_price=Decimal('100'),entry_cap=Decimal('100'),exit_cap=Decimal('100'),
            stop_loss=Decimal('95'),take_profit=Decimal('110'),ttl_minutes=60,
            max_spread_bps=25,max_slippage_bps=25,public_post=public,clock_ns=time.time_ns)
    assert (recorded['public'],recorded['private'])==prior_calls and not recorded['sends']
    vault.lock()
