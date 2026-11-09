"""Empty owned installation, actual app/workers, controlled daily public market."""

import json
import os
from pathlib import Path
import time

from pydantic import BaseModel, ConfigDict

ROOT = Path(os.environ['ORBIS_BASIC_DIRECTORY']).resolve()
assert ROOT.name == 'data' and os.environ.get('ORBIS_BASIC_FIXTURE') == '1'
assert os.environ.get('ORBIS_BASIC_PHASE') in {'create', 'reopen'}
for name in ('ORBIS_LAB_PATH', 'ORBIS_AUTOMATION_PATH', 'ORBIS_SPOT_STRATEGY_PATH',
             'ORBIS_PAPER_PERP_PATH', 'ORBIS_REAL_VAULT_PATH'):
    assert Path(os.environ[name]).resolve().is_relative_to(ROOT)
assert os.environ['ORBIS_DATABASE_URL'] == 'sqlite://'
ROOT.mkdir(parents=True, exist_ok=True)
DAY = 86400
STARTED = time.time_ns()
clock_file = ROOT / 'clock.json'
if clock_file.exists():
    state = json.loads(clock_file.read_text())
else:
    assert os.environ['ORBIS_BASIC_PHASE'] == 'create'
    assert not any((ROOT / name).exists() for name in ('lab.db', 'automation.db', 'real.db'))
    end = 1735948800  # 2025-01-04 00:00 UTC; both PAPER ticks stay in January.
    state = {'clock_ns': (end + 3600) * 10**9, 'end': end,
             'start': end - 6 * DAY, 'ticks': 0}
    clock_file.write_text(json.dumps(state))


def clock_ns():
    return state['clock_ns']


def candle(stamp):
    price = 100 - 5 * ((stamp - state['start']) // DAY)
    return {'t': stamp, 'o': str(price), 'h': str(price + 1),
            'l': str(price - 1), 'c': str(price), 'v': '10'}


from app.lab import api as lab_api
from app.marketdata.adapters.mexc import MexcAdapter


class OwnedMarket(MexcAdapter):
    def get_candles(self, symbol, *, timeframe, limit, since=None, before=None, **_):
        assert symbol == 'BTC/USDT' and timeframe == '1d'
        end = min(before or clock_ns() // 10**9 // DAY * DAY,
                  clock_ns() // 10**9 // DAY * DAY)
        start = since if since is not None else end - limit * DAY
        return [candle(stamp) for stamp in range(max(start, state['start']), end, DAY)][:limit]

    def get_paper_quote(self, symbol):
        assert symbol == 'BTC/USDT'
        return {'symbol': symbol, 'source': 'mexc-spot-public',
                'event_time_ns': clock_ns(), 'bid': '74', 'ask': '75', 'last': '74'}


lab_api.MexcAdapter = OwnedMarket
from app import main
from app.lab.access import lab_access
from app.automation.access import automation_access
key_origin = clock_ns()
def key_clock():
    return (clock_ns() - key_origin) / 10**9
lab_access._clock = automation_access._clock = key_clock

main._paper_dca.adapter_factory = OwnedMarket
main._paper_dca.clock_ns = clock_ns
main._paper_dca_worker.clock_ns = clock_ns
app = main.app


@app.get('/__test/state')
def fixture_state():
    return {'fixture_id': ROOT.parent.name, **state,
            'server': {'pid': os.getpid(), 'started': STARTED},
            'worker': main._paper_dca_worker.snapshot()}


class Advance(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fixture_id: str
    tick: int


@app.post('/__test/advance')
def advance(body: Advance):
    assert body.fixture_id == ROOT.parent.name
    assert body.tick == state['ticks'] + 1 and body.tick <= 2
    state['ticks'] = body.tick
    state['clock_ns'] = (state['end'] + body.tick * DAY + 1) * 10**9
    clock_file.write_text(json.dumps(state))
    main._paper_dca_worker.wake()
    return fixture_state()
