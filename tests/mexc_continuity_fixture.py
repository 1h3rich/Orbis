"""Tiempo y proveedor controlados; observa cada cierre, no salta históricos."""

from datetime import datetime,timezone,timedelta

from app.live_execution.mexc_bot_supervisor import MexcBotSupervisor
from tests.mexc_bot_fixture import make_environment,make_app
from tests.test_spot_bot_market import Driven


def prepare_tape(env,monkeypatch=None):
    original = env["paper"].adapter_factory()
    env["quote"] = {"bid":"65000","ask":"65001"}
    class Tape:
        def get_candles(self,symbol,*,since,before,limit,**kwargs):
            old = original.get_candles(symbol,since=since,before=min(before,env["hour"]),limit=1000)
            now = int(env["clock"]().timestamp())
            rows = list(old)
            start = max(env["hour"],(since+3599)//3600*3600)
            for stamp in range(start,min(before,now//3600*3600+1),3600):
                bearish = stamp >= env["hour"]+25*3600
                rows.append({"t":stamp,"o":"65000" if bearish else "64999",
                    "c":"64999" if bearish else "65000","h":"65001","l":"64998","v":"10"})
            return sorted(rows,key=lambda row:row["t"])[:limit]
    env["paper"].adapter_factory = Tape
    def quote(symbol):
        return {"symbol":symbol,"closeTime":int(env["clock"]().timestamp()*1000),
            "bidPrice":env["quote"]["bid"],"askPrice":env["quote"]["ask"],"lastPrice":env["quote"]["bid"]}
    env["venue"].quote_get = quote
    from app.portfolio import repository
    class ScenarioDatetime(datetime):
        @classmethod
        def now(cls,tz=None):
            value = env["clock"]()
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    if monkeypatch is not None: monkeypatch.setattr(repository,"datetime",ScenarioDatetime)
    else: repository.datetime = ScenarioDatetime


def make_continuity_environment(path,monkeypatch):
    env,workers = make_environment(path,monkeypatch),[]
    prepare_tape(env,monkeypatch)
    def factory(*args,**kwargs):
        worker = Driven(*args,**kwargs); workers.append(worker); return worker
    return env,workers,make_app(env,supervisor_factory=factory)


def observe_hour(env,worker,hour):
    # Un avance indivisible frente al ciclo de fondo, sólo en el escenario.
    with worker.vault.guard_session_generation(worker.vault.session_generation):
        target = datetime.fromtimestamp(env["hour"]+hour*3600+1,timezone.utc)
        env["clock"].offset = target-datetime.now(timezone.utc)
        return worker.run_once()


def make_dca_continuity_environment(path,monkeypatch):
    """Treinta días de mercado y ventanas privadas válidas, sólo pruebas."""
    from dataclasses import replace
    from uuid import uuid4
    from app.live_execution.journal import register_spot_policy
    from tests.mexc_dca_fixture import DcaScenario
    from tests.test_mexc_execution_state import PASSWORD
    scenario = DcaScenario(path,monkeypatch)
    scenario.rows.extend({"t":scenario.day+day*86400,"o":"57000","h":"57010",
                          "l":"56990","c":"57000","v":"10"} for day in range(3,33))
    scenario.draft = replace(scenario.draft,policy_id=str(uuid4()),expires_at=scenario.clock()+timedelta(days=31))
    scenario.env["vault"].unlock(PASSWORD)
    register_spot_policy(scenario.env["vault"],scenario.draft,now=scenario.clock())
    scenario.env["vault"].lock()
    window = scenario.env["venue"].trades_window
    def recent_trades(*args,**kwargs):
        result = window(*args,**kwargs)
        result["trades"] = [fill for fill in result["trades"]
            if result["start_ms"] <= fill["time_ms"] <= result["end_ms"]]
        return result
    scenario.env["venue"].trades_window = recent_trades
    return scenario
