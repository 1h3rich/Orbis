"""Servidor con supervisor REAL y transporte ficticio; bases aisladas."""

import os
import time
from datetime import datetime,timezone
from pathlib import Path

from app.portfolio import api as real_api
from app.lab.api import create_lab_router
from app.live_execution.mexc_bot_supervisor import MexcBotSupervisor
from app.web.mount import register_ui
from tests.mexc_bot_fixture import make_environment, make_app, advance_signal,advance_market
from app.marketdata import service as market_service
from app.live_execution import mexc_spot_bot as bot_executor
from app.live_execution import mexc_order_lifecycle


environment = make_environment(Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"]))
CONTINUOUS = os.environ.get("ORBIS_TEST_CONTINUOUS") == "1"
if CONTINUOUS:
    from tests.mexc_continuity_fixture import prepare_tape
    prepare_tape(environment)
workers, quote = [], environment.get("quote",{"bid":"65000","ask":"65001"})
cycle_errors = []
fault = {"mode":"none"}
_submit = bot_executor.submit_anchored_spot_limit


def controlled_submit(*args,**kwargs):
    mode = fault["mode"]
    fault["mode"] = "none"
    if mode == "before_submit":
        raise RuntimeError("fixture interruption after atomic decision/reservation")
    if mode == "legacy":
        from tests.test_spot_bot_preparation import remove_reservation_as_legacy_fixture

        remove_reservation_as_legacy_fixture(workers[-1].vault)
        fault["legacy_intent"] = kwargs["bot_intent"]
        raise RuntimeError("fixture legacy decision without local reservation")
    previous = environment["trader"].timeout_after_accepting
    environment["trader"].timeout_after_accepting = mode == "timeout"
    try:
        return _submit(*args,**kwargs)
    finally:
        environment["trader"].timeout_after_accepting = previous


bot_executor.submit_anchored_spot_limit = controlled_submit


class ScenarioDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = environment["clock"].value
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


# El escenario adelanta una vela sin esperar una hora real. La revisión de
# saldo usa EL MISMO reloj controlado que View, PAPER y el motor; sus guardas
# de frescura permanecen intactas. Sólo afecta a este proceso de pruebas.
_assess = real_api.assess_mexc_spot_parity
real_api.assess_mexc_spot_parity = lambda *args,**kwargs: _assess(*args,now=environment["clock"](),**kwargs)
real_api.datetime = ScenarioDatetime
mexc_order_lifecycle.datetime = ScenarioDatetime

if any(os.environ.get(mode) == "1" for mode in ("ORBIS_TEST_MEXC_MANUAL_REARM", "ORBIS_TEST_MEXC_SALE_REARM")):
    from functools import partial

    real_api.activate_spot_execution = partial(real_api.activate_spot_execution, clock=environment["clock"])
    real_api.submit_anchored_spot_limit = partial(real_api.submit_anchored_spot_limit, clock=environment["clock"])



def quote_get(symbol):
    return {"symbol":symbol,"closeTime":int(environment["clock"].value.timestamp()*1000),
            "bidPrice":quote["bid"],"askPrice":quote["ask"],"lastPrice":quote["bid"]}


environment["venue"].quote_get = quote_get


def factory(*args,**kwargs):
    def cycle(*cycle_args,**cycle_kwargs):
        try: return bot_executor.run_mexc_spot_bot_once(*cycle_args,**cycle_kwargs)
        except Exception as exc:
            cycle_errors.append({"type":type(exc).__name__,"detail":str(exc)})
            raise
    worker = MexcBotSupervisor(*args,interval_seconds=0.1,cycle=cycle,**kwargs)
    workers.append(worker)
    return worker


app = make_app(environment,supervisor_factory=factory)
app.include_router(create_lab_router(environment["lab"].path))
register_ui(app)


def chart_candles(symbol,*,provider,timeframe,limit,since=None,before=None):
    assert provider == "mexc" and symbol == "BTC/USDT" and timeframe == "1h" and limit == 1000
    now = int(environment["clock"].value.timestamp())
    first = now//3600*3600-12*3600 if before is None else before-6*3600
    rows = [{"t":first+i*3600,"o":"65000","h":"73000","l":"64000","c":"66000","v":"10"}
            for i in range(13 if before is None else 6) if first+i*3600 <= now]
    for row in rows:
        row["closed"] = row["t"]+3600 <= now
    return {"available":True,"resolved_provider":"mexc","symbol":symbol,"timeframe":timeframe,
            "candles":rows,"older_pagination_supported":True,"history_exhausted":before is not None}


market_service.candles = chart_candles  # Sólo en este proceso aislado, nunca HTTP a un proveedor real.


@app.post("/__test/entry")
def entry():
    return {"poll_result":advance_signal(environment,workers=workers)["poll_result"]}


@app.post("/__test/market-only")
def market_only():
    # Un salto de reloj de hasta una hora no puede atravesar la mitad de una
    # lectura privada del escenario. Producción conserva sus guardas de tiempo.
    vault = workers[-1].vault
    with vault.guard_session_generation(vault.session_generation):
        advance_market(environment)
    return {"changed":True}


@app.post("/__test/close-paper")
def close_paper():
    environment["paper"].stop(environment["session"]["id"])
    if CONTINUOUS: environment["paper"].passphrase = None
    return {"changed":True}


@app.post("/__test/drive-to/{hour}")
def drive(hour:int):
    assert CONTINUOUS and 1 <= hour <= 72
    from app.live_execution.spot_bot_store import bot_record
    worker = workers[-1]; vault = worker.vault
    current = (int(environment["clock"]().timestamp())-environment["hour"])//3600
    for tick in range(current+1,hour+1):
        with vault.guard_session_generation(vault.session_generation):
            target = datetime.fromtimestamp(environment["hour"]+tick*3600+1,timezone.utc)
            environment["clock"].offset = target-datetime.now(timezone.utc)
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            with vault.session() as db:
                row,_,state = bot_record(db,worker.bot_id)
                finished = row.cycle_id is None and state["last_candle_t"] >= environment["hour"]+(tick-1)*3600
            if finished: break
            assert worker.state()["running"],cycle_errors
            time.sleep(0.01)
        else: raise AssertionError({"hour":tick,"worker":worker.state(),"errors":cycle_errors})
    return {"hour":hour,"orders":len(environment["trader"].posts),"worker":worker.state()}


@app.post("/__test/fault/{mode}")
def set_fault(mode: str):
    assert mode in ("none","before_submit","timeout","legacy")
    fault["mode"] = mode
    return {"fixture_mode":mode}


@app.post("/__test/legacy-found")
def expose_legacy_order(canceled: bool = False):
    intent = fault.pop("legacy_intent")
    if canceled:
        from tests.test_spot_bot_preparation import add_external_cancel_as_legacy_fixture
        fault["legacy_cancel_order"] = add_external_cancel_as_legacy_fixture(environment["trader"],intent)
    else:
        environment["trader"].place_limit_order(intent)
    return {"fixture_order":intent.intent_id}


@app.post("/__test/legacy-status/{status}")
def change_legacy_status(status: str):
    assert status in ("NEW", "CANCELED")
    fault["legacy_cancel_order"]["status"] = status
    return {"status":status}


@app.post("/__test/expire-continuity")
def expire_continuity():
    assert CONTINUOUS and workers
    from app.live_execution.spot_bot_store import bot_record
    from datetime import timedelta
    worker = workers[-1]; vault = worker.vault
    with vault.guard_session_generation(vault.session_generation):
        with vault.session() as db:
            _,config,_ = bot_record(db,worker.bot_id)
            end = datetime.fromisoformat(config["execution_permission"]["expires_at"])
        environment["clock"].offset = end+timedelta(seconds=1)-datetime.now(timezone.utc)
    assert worker.join(5),cycle_errors
    return {"worker":worker.state(),"orders":len(environment["trader"].posts)}


@app.post("/__test/target")
def target():
    quote.update(bid="72000",ask="72001")
    return {"changed":True}


@app.get("/__test/posts")
def posts():
    return {"count":len(environment["trader"].posts),"workers":[worker.state() for worker in workers],"errors":cycle_errors,
            "fill_times":[row["time_ms"] for row in environment["venue"].trades],
            "fill_sizes":[row["quantity"] for row in environment["venue"].trades],
            "venue_base":environment["venue"].base}


@app.post("/__test/corrupt-market")
def corrupt_market():
    assert workers and not environment["trader"].posts
    from app.portfolio.models import RealSpotBotMarketStream
    with workers[-1].vault.session() as db:
        row = db.query(RealSpotBotMarketStream).one()
        row.seed_digest = "0"*64
        db.commit()
    return {"changed":True}

@app.post("/__test/add-cash")
def add_cash():
    from decimal import Decimal
    from uuid import uuid4
    from app.live_execution.spot_bot_store import bot_record
    from app.portfolio.domain import EventInput
    from app.portfolio.repository import append_event

    assert os.environ.get("ORBIS_TEST_MEXC_CASH_REARM")=="1" and len(workers)==1
    worker=workers[0]
    with worker.vault.session() as db:
        row, _, _=bot_record(db,worker.bot_id)
        assert row.status=="PAUSED"
        append_event(db,row.account_id,str(uuid4()),EventInput(kind="CONTRIBUTION",quote_currency="USDT",
            amount=Decimal("10"),occurred_at=environment["clock"]()))
    venue=environment["venue"]
    venue.quote=str(Decimal(venue.quote)+10)
    return {"cash_added":"10"}
