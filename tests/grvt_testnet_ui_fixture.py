"""PAPER/LAB reales cifrados, firma real con clave sintética y transporte sin red."""

from decimal import Decimal
from functools import partial
from unittest.mock import patch
import time

from eth_account import Account
from fastapi import FastAPI
import httpx

from app.derivatives.paper_migration import encrypt_active_paper_perp
from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_math import PerpQuote
from app.lab.access import lab_access
from app.lab.migration import encrypt_active_lab
from app.lab.engine import validate_candles
from app.live_execution.grvt_testnet_order import AUTH_URL, ORDER_URL, GrvtTestnetOrderClient
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
from app.live_execution.grvt_testnet_signal import sign_and_submit_signal_testnet_once, submit_signal_testnet_once
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument
from app.live_execution.grvt_testnet_submit import submit_testnet_once
from app.portfolio.api import create_real_router
from tests import test_signal_monitor as signal_tests


KEY = "0x" + "33" * 32  # Nunca una credencial del usuario.
SIGNER = Account.from_key(KEY).address
BUILDER = "0x" + "a" * 40
PASSPHRASE = "GRVT isolated browser password"


class GrvtTestnetScenario:
    def __init__(self, directory):
        self.directory = directory
        self.start = (int(time.time()) // 3600) * 3600 - 6 * 3600
        with patch.object(signal_tests, "START", self.start):
            env = signal_tests.harness(directory)
        self.monitor, _, self.clock, self.paper, self.lab, _, self.dataset, _, _ = env
        self.rows = env[-1]
        encrypt_active_paper_perp(self.paper, "CIFRAR GRVT PAPER", PASSPHRASE)
        encrypt_active_lab(self.lab, "CIFRAR LAB", PASSPHRASE)
        paper_perp_access.unlock(self.paper.path, PASSPHRASE)
        lab_access.unlock(self.lab.path, PASSPHRASE)
        self.calls, self.orders = [], []
        self.lose_response = False
        self.serial = 0
        self.held = {}

    def seed(self, direction="SHORT", *, closing=False, defer_open=False, research=False):
        assert direction in ("LONG", "SHORT")
        # Una sola referencia horaria para el bootstrap y la observación.
        # El cifrado/setup puede cruzar de hora desde __init__: no presentar
        # entonces dos barras no observadas como una entrada PAPER fresca.
        seed_now = int(time.time())
        activation = seed_now//3600*3600-3600+10
        if research:activation-=3600
        bootstrap = [row for row in self.rows if row['t'] < activation//3600*3600][-5:]
        bars,gaps = validate_candles(bootstrap,'1h',now=activation)
        assert (len(bars) >= 4 if research else len(bars) == 5) and not gaps
        seed_dataset = self.lab.save_dataset('BTC/USDT:PERP','1h','grvt-perp-trade-public',
            bars,[],verified_public=True)
        self.serial += 1
        account = self.paper.create_account(direction + " " + str(self.serial), "1000", "500", "800", "5", 2, "0.001", "0")
        definition=signal_tests.definition(direction)
        if research:definition={**definition,'entry':{'kind':'BEARISH_STREAK','period':len(bars)+2}}
        strategy = self.lab.create_strategy(direction + " causal " + str(self.serial), definition)
        self.clock["now"] = activation
        run=None;validation=None
        if research:
            from app.lab.engine import CURRENT_ENGINE_VERSION,run_backtest
            from tests.grvt_research_fixture import historical_grvt_reserve
            run=self.lab.save_run(seed_dataset['id'],strategy['version_id'],
                run_backtest(bars,strategy['definition'],'1h'),CURRENT_ENGINE_VERSION)
            validation=historical_grvt_reserve(self.lab,strategy['version_id'],seed_dataset['start_t'])
        session = self.monitor.create(account["id"], strategy["version_id"], seed_dataset["id"], "AUTO_PAPER", "1", "2",
            source_run_id=run['id'] if run else None)
        if research:
            self.clock['now']=activation+3600
            observed=self.monitor.poll(session['id'])
            assert observed['poll_result']=='OBSERVED' and not observed['actions']
        if defer_open:
            return {'session_id': session['id'], 'sub_account_id': str(123456+self.serial),
                'direction': direction, 'closing': False,
                **({'research_validation_id':validation['id']} if validation else {})}
        self.clock["now"] = seed_now
        self.monitor.poll(session["id"])
        subaccount = str(123456 + self.serial)
        if closing:
            # Cierre de riesgo observado realmente por el motor PAPER, no UPDATE de una acción.
            mark = Decimal("110") if direction == "LONG" else Decimal("80")
            assert self.observe_risk_mark(session['id'],mark) == 'EXECUTED'
            self.held[subaccount] = "0.5" if direction == "LONG" else "-0.5"
        with self.paper.connect() as db:
            action = db.execute("SELECT request_id FROM paper_signal_actions WHERE session_id=? AND action=?",
                                (session["id"],"CLOSE" if closing else "OPEN")).fetchone()
        assert action is not None
        from app.live_execution.grvt_testnet_preflight import client_id_for_signal
        return {"session_id":session["id"], "action_request_id":action["request_id"],
                "client_order_id":client_id_for_signal(session["id"], action["request_id"]),
                "sub_account_id":subaccount, "direction":direction, "closing":closing}

    def observe_risk_mark(self,session_id,mark):
        # Una referencia para el ticker y el reloj inyectado del observador.
        # El setup cifrado puede tardar segundos bajo carga; seed no es "ahora".
        observed_at=time.time_ns()
        self.clock['now']=observed_at//1_000_000_000
        return self.monitor.on_risk_quote(session_id,PerpQuote('BTC_USDT_Perp',observed_at,
            mark,mark-Decimal('0.01'),mark+Decimal('0.01')))

    def response(self, url, payload):
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url),
            headers={"X-Grvt-Account-Id":BUILDER, "Set-Cookie":"gravity=fake-testnet-cookie; Path=/"} if url == AUTH_URL else {})

    def factory(self, key):
        assert key.startswith("fake-testnet-key-")
        sub = key.removeprefix("fake-testnet-key-")

        def post(url, body, headers):
            assert "testnet.grvt.io" in url
            self.calls.append(url)  # No se guardan claves, cookies ni firmas en el log de prueba.
            if url == AUTH_URL:
                return self.response(url, {"status":"success", "sub_account_id":sub})
            if url.endswith("/account_summary"):
                held = self.held.get(sub)
                return self.response(url, {"result":{"sub_account_id":sub,
                    "event_time":str(time.time_ns()), "settle_currency":"USDT", "margin_type":"SIMPLE_CROSS_MARGIN",
                    "sub_account_mode":"SINGLE_ASSET_MODE", "total_equity":"10000", "initial_margin":"0",
                    "maintenance_margin":"0", "unrealized_pnl":"0",
                    "positions":[{"sub_account_id":sub,"instrument":"BTC_USDT_Perp","size":held}] if held else [],
                    "spot_balances":[{"currency":"USDT", "balance":"10000"}]}})
            if url.endswith("/open_orders"):
                return self.response(url, {"result":[]})
            assert url == ORDER_URL
            order = body["order"]
            self.orders.append({"id":order["metadata"]["client_order_id"], "side":"BUY" if order["legs"][0]["is_buying_asset"] else "SELL",
                                "reduce_only":order["reduce_only"], "chain_id":order["signature"]["chain_id"]})
            if self.lose_response:
                raise httpx.ReadTimeout("fake lost provider reply")
            return self.response(url, {"result":{**order,"order_id":"0xfake" + str(len(self.orders))}})
        return GrvtTestnetOrderClient(key, http_post=post)

    def public_rules(self, url, body):
        assert "testnet.grvt.io" in url
        self.calls.append(url)
        return self.response(url, {"result":{"instrument":"BTC_USDT_Perp", "max_position_size":"1000",
            "risk_brackets":[{"tier":1,"notional_floor":"0","notional_cap":None,
                "initial_margin_rate":"0.02", "maintenance_margin_rate":"0.01", "max_leverage":50,
                "cumulative_maintenance_amount":"0"}]}})

    def signed_submit(self, *args, **kwargs):
        return sign_and_submit_signal_testnet_once(*args, **kwargs,
            instrument_fetcher=lambda _: GrvtTestnetInstrument("BTC_USDT_Perp",123456789,3,Decimal("0.01"),Decimal("0.001"),Decimal("10")),
            quote_fetcher=lambda _: GrvtTestnetQuote("BTC_USDT_Perp",time.time_ns(),time.time_ns(),Decimal("100"),Decimal("100.01"),Decimal("10"),Decimal("10")),
            signal_submitter=lambda *a, **k: submit_signal_testnet_once(*a, **k,
                submitter=partial(submit_testnet_once, public_post=self.public_rules)))

    def make_app(self):
        app = FastAPI()
        app.include_router(create_real_router(self.directory / "real.db", lab_path=self.lab.path,
            paper_perp_path=self.paper.path, grvt_testnet_factory=self.factory,
            grvt_testnet_signal_signed_submitter=self.signed_submit))
        return app

    def body(self, source):
        return {"api_key":"fake-testnet-key-" + source["sub_account_id"], "dedicated_private_key":KEY,
            "expected_signer":SIGNER, "builder":BUILDER, "session_id":source["session_id"],
            "action_request_id":source["action_request_id"], "sub_account_id":source["sub_account_id"],
            "instrument":"BTC_USDT_Perp", "max_order_notional":"100", "size":"0.5", "limit_price":"100.01",
            "side":"BUY" if (source["direction"] == "LONG") != source["closing"] else "SELL", "reduce_only":source["closing"],
            "max_spread_bps":25, "max_slippage_bps":25, "confirmation":"FIRMAR_Y_ENVIAR_GRVT_TESTNET"}
