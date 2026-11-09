"""Isolated browser server using the real GRVT LIMIT API with owned data."""
import os
from pathlib import Path
from threading import Event

directory = Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY']).resolve()
assert 'verification' in directory.parts

from scripts.verify_orbis import environment
os.environ.update(environment(directory/'bootstrap'))

import pytest
from app.derivatives.perp_observed_api import create_observed_paper_router
from app.derivatives.perp_observed_service import ObservedPaperService
from app.marketdata import service as market_service
from app.web.mount import register_ui
from tests.test_grvt_perp_limit_api import limit_fixture
from tests.test_grvt_perp_limit_execution import SUB

drive = Event()


def wait_for_signal(stop):
    while not drive.wait(.02):
        if stop.is_set():
            return


fixture = limit_fixture(directory/'owned',pytest.MonkeyPatch(),hold_pending=True,
                        on_wait=wait_for_signal,recover_from_history=True,
                        partial=os.environ.get('ORBIS_TEST_GRVT_LIMIT_PARTIAL')=='1')
owned = fixture.__enter__()
app = owned.app
origin = owned.send['origin']
app.include_router(create_observed_paper_router(ObservedPaperService(origin.origin.lab, origin.archive)))
register_ui(app)


def candles(symbol,*,provider,timeframe,limit,since=None,before=None):
    assert provider == 'grvt' and symbol == 'BTC/USDT:PERP' and limit == 1000
    step = {'1m':60,'1h':3600,'4h':14400}[timeframe]
    now = owned.case.clock[0]//10**9
    first = now//step*step-12*step if before is None else before-6*step
    return {'available':True,'resolved_provider':'grvt','symbol':symbol,'timeframe':timeframe,
        'candles':[{'t':first+i*step,'o':'100','h':'104','l':'97','c':'101','v':'10',
                    'closed':first+(i+1)*step<=now} for i in range(13 if before is None else 6)],
        'older_pagination_supported':True,'history_exhausted':before is not None}


market_service.candles = candles


@app.get('/__test/state')
def state():
    return {'fixture_id':directory.name,'session_id':owned.session_id,'partial':owned.partial,
            'entry_posts':sum(bool(call['orders']) for call in owned.calls),
            'cancellations':sum(not call['orders'] for call in owned.calls),'exit_posts':len(owned.posts),
            'order_history_pages':sum(name == 'order_history' for name,_ in owned.case.settings['history_requests']),
            'at_pending':owned.at_pending.is_set(),'job':owned.bots.limit_status(SUB),'running':owned.bots.active_sub(SUB),
            'positions':owned.case.settings['positions']}


@app.post('/__test/emit')
def emit():
    drive.set()
    return {'ok':True}
