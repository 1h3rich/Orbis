"""LAB/PAPER cifrado y exchange ficticio para API/UI de bots; cero red privada."""

from dataclasses import replace
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from uuid import uuid4

from fastapi import FastAPI

from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.engine import CURRENT_ENGINE_VERSION, run_backtest, validate_candles
from app.lab.store import LabStore
from app.live_execution.journal import register_spot_policy
from app.portfolio.api import create_real_router
from tests import test_mexc_execution_state as fixtures
from tests.test_mexc_execution import ExchangeOrders
from tests.test_mexc_execution_api import Probe
from tests.test_spot_strategy_paper import definition


class MutableRealtimeClock:
    def __init__(self):
        self.offset = timedelta(0)

    @property
    def value(self):
        return datetime.now(timezone.utc) + self.offset

    def __call__(self):
        return self.value


def make_environment(directory, monkeypatch=None):
    account_ref = hmac.new(b"fixture-secret",b"fixture-key",hashlib.sha256).hexdigest()
    if monkeypatch is None:
        fixtures.Clock, fixtures.ACCOUNT = MutableRealtimeClock, account_ref
    else:
        monkeypatch.setattr(fixtures,"Clock",MutableRealtimeClock)
        monkeypatch.setattr(fixtures,"ACCOUNT",account_ref)
    vault, template, account, venue, clock = fixtures.setup(directory)
    hour = int(clock.value.timestamp()) // 3600 * 3600
    pairs = [(100,99),(100,99),(100,99),(99,100),(98,99)]
    rows = [{"t":hour + (i - 4) * 3600,"o":str(o),"h":str(max(c,o)+1),"l":str(min(c,o)-1),
             "c":str(c),"v":"10"} for i,(c,o) in enumerate(pairs)]
    paper_time = {"now":hour - 3600 + 1}

    class Source:
        def get_candles(self, symbol, *, since, before, limit, **_):
            return [row for row in rows if since <= row["t"] < before][:limit]

        def get_paper_quote(self, symbol):
            return {"symbol":symbol,"source":"mexc-spot-public","event_time_ns":paper_time["now"]*1_000_000_000,
                    "bid":"99","ask":"100","last":"99"}

    lab = LabStore(directory / "lab.db")
    bars, gaps = validate_candles(rows[:3],"1h",now=paper_time["now"])
    dataset = lab.save_dataset("BTC/USDT","1h","mexc-spot-public",bars,gaps,verified_public=True)
    strategy = lab.create_strategy("Bot nativo de prueba",{**definition(),"entry":{"kind":"BEARISH_STREAK","period":2}})
    saved = lab.save_run(dataset["id"],strategy["version_id"],
                         run_backtest(bars,strategy["definition"],"1h"),CURRENT_ENGINE_VERSION)
    from tests.mexc_research_fixture import historical_reserve
    validation = historical_reserve(lab,strategy['version_id'],before_t=dataset['start_t'])
    paper = SpotStrategyPaper(directory / "spot.db",lab.path,adapter_factory=Source,
                              clock_ns=lambda:paper_time["now"]*1_000_000_000,passphrase=fixtures.PASSWORD)
    session = paper.create(strategy["version_id"],dataset["id"],saved["id"])
    paper_time["now"] = hour + 1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    draft = replace(template,policy_id=str(uuid4()),strategy_version_id=strategy["version_id"],enabled=False,
                    reconciliation_anchor_id=None,expires_at=clock.value+timedelta(days=7))
    register_spot_policy(vault,draft,now=clock())
    vault.lock()
    trader = ExchangeOrders(venue,clock)
    return {"vault":vault,"draft":draft,"account":account,"venue":venue,"clock":clock,"paper":paper,
            "session":session,"trader":trader,"hour":hour,"paper_time":paper_time,"lab":lab,'validation':validation}


def make_app(environment, *, supervisor_factory=None):
    from tests.mexc_cancel_fixture import controlled_view

    env = environment
    app = FastAPI()
    options = {"mexc_bot_supervisor_factory":supervisor_factory} if supervisor_factory else {}
    app.include_router(create_real_router(env["vault"].path,lab_path=env["lab"].path,spot_paper=env["paper"],
        mexc_factory=lambda key,secret:controlled_view(env["venue"],key,secret),mexc_trade_factory=Probe,mexc_live_factory=lambda *_:env["trader"],
        mexc_quote_get=env["venue"].quote_get,mexc_rules_fetch=lambda symbol,now:env["venue"].rules_fetch(symbol,now),
        mexc_bot_clock=env["clock"],**options))
    return app


def advance_signal(env, *, workers=()):
    # El salto artificial debe quedar entre ciclos. Mismo orden de cerrojos
    # que el motor: REAL antes de PAPER. No se cambian guardas de producción.
    with ExitStack() as guards:
        guarded=set()
        for worker in workers:
            if id(worker.vault) in guarded: continue
            guarded.add(id(worker.vault))
            generation=worker.vault.session_generation
            if generation is not None:
                guards.enter_context(worker.vault.guard_session_generation(generation))
        advance_market(env)
        return env["paper"].poll(env["session"]["id"])


def advance_market(env):
    """Adelanta datos públicos sin imponer que PAPER alcance primero al bot REAL."""
    target = datetime.fromtimestamp(env["hour"]+3600+1,timezone.utc)
    env["clock"].offset = target - datetime.now(timezone.utc)
    env["paper_time"]["now"] = int(env["clock"].value.timestamp())
