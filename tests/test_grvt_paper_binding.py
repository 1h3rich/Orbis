"""La prueba causal PAPER/LAB es común; no autoriza ninguna red de ejecución."""
import time
from tests.grvt_testnet_ui_fixture import GrvtTestnetScenario


def test_shared_paper_binding_replays_real_history_without_any_exchange_call(tmp_path):
    from app.live_execution.grvt_paper_binding import reviewed_paper_signal
    origin=GrvtTestnetScenario(tmp_path);source=origin.seed('LONG')
    with reviewed_paper_signal(origin.paper,origin.lab,instrument='BTC_USDT_Perp',
            session_id=source['session_id'],action_request_id=source['action_request_id'],side='BUY',
            reduce_only=False,now_ns=time.time_ns(),clock_ns=time.time_ns) as (binding,proof,definition,at,clock):
        assert binding.action=='OPEN' and proof['dataset_hash'] and definition['direction']=='LONG'
        assert clock()>=at and not origin.calls and not origin.orders
