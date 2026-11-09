"""DCA probado → PAPER cifrado → órdenes MEXC ficticias con saldo compartido."""

from dataclasses import replace
from datetime import datetime,timedelta,timezone
from uuid import uuid4

from fastapi import FastAPI

from app.automation.access import automation_access
from app.automation.dca import dca_engine_for_rule, simulate_dca, validate_dca
from app.automation.paper_dca import PaperDca
from app.automation.store import AutomationStore
from app.lab.engine import validate_candles
from app.lab.store import content_hash
from app.live_execution.journal import register_spot_policy
from app.live_execution.mexc_bot_supervisor import MexcBotSupervisor
from app.portfolio.api import create_real_router
from tests.mexc_roundtrip_fixture import Roundtrip
from tests.test_mexc_execution_api import Probe
from tests.test_mexc_execution_state import PASSWORD


EMA_RULE = {"ma_adjustment": {"kind": "ema", "period": 2, "condition": "below",
                             "matched_factor": "0.25", "unmatched_factor": "0"}}
VOLUME_RULE = {"volume_gate": {"period": 3, "min_ratio": "2"}}


class DcaScenario:
    def __init__(self,directory,monkeypatch=None,*,average_rule=None,volume=False):
        self.base = Roundtrip(directory,monkeypatch)
        self.env,self.clock,self.workers = self.base.env,self.base.clock,[]
        self._raw_trade_window=self.env['venue'].trades_window
        self.env['venue'].trades_window=self.trades_window
        self.day = int(self.clock().timestamp())//86400*86400
        self.seed_count = 6 if volume else 3
        prices = [65000]*(self.seed_count+1) + [57000,65000 if average_rule else 50000]
        volumes = [10,10,10,20,10,10,10,20,5] if volume else [10]*len(prices)
        self.rows = [{"t":self.day+(i-self.seed_count)*86400,"o":str(price),"h":str(price+10),"l":str(price-10),
                      "c":str(price),"v":str(volumes[i])} for i,price in enumerate(prices)]
        self.candle_requests = []
        scenario = self
        class Source:
            def get_candles(self,symbol,timeframe="1d",limit=3,since=None,before=None):
                scenario.candle_requests.append((limit,since,before))
                return [row.copy() for row in scenario.rows if since <= row["t"] < before][:limit]
            def get_paper_quote(self,symbol):
                return {"symbol":symbol,"source":"mexc-spot-public","event_time_ns":int(scenario.clock().timestamp()*1e9),
                        "bid":scenario.base.quote["bid"],"ask":scenario.base.quote["ask"],"last":scenario.base.quote["bid"]}
        path = directory/"dca.db"
        automation_access.create(path,PASSWORD)
        store = AutomationStore(path,allow_create=False)
        rule = validate_dca({"frequency":"daily","amount":"100","budget_total":"1000","monthly_limit":"300",
            "max_exposure":"1","fee_rate":"0.001","slippage_rate":"0.002","max_extra_activations":2,
            "cooldown_bars":0,"dips":[{"drop_pct":"0.1","amount":"50"},{"drop_pct":"0.2","amount":"100"}],
            **(VOLUME_RULE if volume else average_rule or {})})
        self.version = store.create_rule("DCA probado",rule)
        bars,gaps = validate_candles(self.rows[:self.seed_count],"1d",now=int(self.clock().timestamp()))
        self.dataset = self.env["lab"].save_dataset("BTC/USDT","1d","mexc-spot-public",bars,gaps,verified_public=True)
        run = store.save_run(self.version["version_id"],self.dataset["id"],self.dataset["hash"],simulate_dca(bars,rule,timeframe="1d"),
            dca_engine_for_rule(rule),lab_path=self.env["lab"].path,expected_definition_hash=content_hash(rule))
        from tests.mexc_research_fixture import historical_reserve
        self.validation = historical_reserve(self.env['lab'],self.version['version_id'],
            before_t=self.dataset['start_t'],timeframe='1d',automation=store)
        self.paper = PaperDca(path,self.env["lab"].path,adapter_factory=Source,
                             clock_ns=lambda:int(self.clock().timestamp()*1e9))
        self.session = self.paper.create(self.version["version_id"],self.dataset["id"],run["id"])
        self.advance(1,seconds=1)
        assert self.paper.poll(self.session["id"])["poll_result"] == "OBSERVED"
        self.advance(1,seconds=30)
        self.draft = replace(self.env["draft"],policy_id=str(uuid4()),strategy_version_id=self.version["version_id"],
                             expires_at=self.clock()+timedelta(days=7))
        vault = self.env["vault"]; vault.unlock(PASSWORD)
        register_spot_policy(vault,self.draft,now=self.clock()); vault.lock()

    def advance(self,days,*,seconds=1):
        target = datetime.fromtimestamp(self.day+days*86400+seconds,timezone.utc)
        # PAPER puede ponerse al día DESPUÉS de una compra REAL. No rebobinar
        # el reloj del exchange/libro al principio del mismo día al hacerlo.
        target = max(target,self.clock())
        self.clock.offset = target-datetime.now(timezone.utc)
        if days >= 2:
            close = int(self.rows[min(days + self.seed_count - 1, len(self.rows) - 1)]["c"])
            self.base.quote.update(bid=str(close),ask=str(close + 1))

    def observe(self,days):
        self.advance(days)
        return self.paper.poll(self.session["id"])

    def trades_window(self,symbol,*,days):
        # El escenario cruza días: el proveedor debe respetar la ventana
        # solicitada, no devolver todas las operaciones desde el inicio.
        snapshot=self._raw_trade_window(symbol,days=days)
        return {**snapshot,'trades':[row for row in snapshot['trades']
            if not isinstance(row,dict) or type(row.get('time_ms')) is not int
            or snapshot['start_ms']<=row['time_ms']<=snapshot['end_ms']]}

    def make_app(self,*,driven=False):
        scenario = self
        class Driven(MexcBotSupervisor):
            def start(self):
                self._generation = self.vault.session_generation
                self.driven_running = True
            def request_stop(self):
                self.driven_running = False
                super().request_stop()
            def state(self):
                return {**super().state(),"running":getattr(self,"driven_running",False) and not self._stop.is_set()}
        def factory(*args,**kwargs):
            cls = Driven if driven else MexcBotSupervisor
            worker = cls(*args,interval_seconds=0.1,**kwargs)
            scenario.workers.append(worker)
            return worker
        app = FastAPI()
        from app.database.database import get_db
        app.dependency_overrides[get_db] = lambda:None
        app.include_router(create_real_router(self.env["vault"].path,lab_path=self.env["lab"].path,
            spot_paper=self.env["paper"],dca_paper=self.paper,mexc_factory=lambda *_:self.env["venue"],mexc_trade_factory=Probe,
            mexc_live_factory=lambda *_:self.env["trader"],mexc_quote_get=self.env["venue"].quote_get,
            mexc_rules_fetch=lambda symbol,now:self.env["venue"].rules_fetch(symbol,now),mexc_bot_clock=self.clock,
            mexc_bot_supervisor_factory=factory))
        return app
