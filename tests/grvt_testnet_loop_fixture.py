"""Cuenta TESTNET ficticia compartida por órdenes, fills, auditoría y saldo."""

from decimal import Decimal
from functools import partial
import time

from app.derivatives.paper_math import PerpQuote
from app.live_execution.grvt_testnet_order import (
    AUTH_URL,ORDER_URL,LOOKUP_URL,FILL_HISTORY_URL,FUNDING_HISTORY_URL,TRANSFER_HISTORY_URL,
    WITHDRAWAL_HISTORY_URL,DEPOSIT_HISTORY_URL,INTEREST_HISTORY_URL,GrvtTestnetOrderClient)
from app.live_execution.grvt_testnet_preflight import client_id_for_signal
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
from app.live_execution.grvt_testnet_signal import sign_and_submit_signal_testnet_once,submit_signal_testnet_once
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument
from app.live_execution.grvt_testnet_submit import submit_testnet_once
from tests.grvt_testnet_ui_fixture import GrvtTestnetScenario


class GrvtTestnetLoop(GrvtTestnetScenario):
    def __init__(self,directory):
        super().__init__(directory)
        self.balance = Decimal("10000")
        self.position = Decimal(0)
        self.price = Decimal("100")
        self.entry_price = Decimal(0)
        self.venue_orders,self.fills = {},[]
        self.funding_rows = []
        self.funding_on_close = Decimal(0)

    def signed_submit(self,*args,**kwargs):
        return sign_and_submit_signal_testnet_once(*args,**kwargs,
            instrument_fetcher=lambda _:GrvtTestnetInstrument("BTC_USDT_Perp",123456789,3,Decimal("0.01"),Decimal("0.001"),Decimal("10")),
            quote_fetcher=lambda _:GrvtTestnetQuote("BTC_USDT_Perp",time.time_ns(),time.time_ns(),self.price-Decimal("0.01"),self.price+Decimal("0.01"),Decimal("10"),Decimal("10")),
            signal_submitter=lambda *a,**k:submit_signal_testnet_once(*a,**k,
                submitter=partial(submit_testnet_once,public_post=self.public_rules)))

    def factory(self,key):
        assert key.startswith("fake-testnet-key-")
        sub = key.removeprefix("fake-testnet-key-")
        def post(url,body,headers):
            assert "testnet.grvt.io" in url
            self.calls.append(url)
            if url == AUTH_URL: return self.response(url,{"status":"success","sub_account_id":sub})
            if url.endswith("/account_summary"):
                upnl = (self.price-self.entry_price)*self.position
                return self.response(url,{"result":{"sub_account_id":sub,"event_time":str(time.time_ns()),
                    "settle_currency":"USDT","margin_type":"SIMPLE_CROSS_MARGIN","sub_account_mode":"SINGLE_ASSET_MODE",
                    "total_equity":str(self.balance+upnl),"initial_margin":"1" if self.position else "0",
                    "maintenance_margin":"0.5" if self.position else "0","unrealized_pnl":str(upnl),
                    "positions":[{"sub_account_id":sub,"instrument":"BTC_USDT_Perp","size":str(self.position)}] if self.position else [],
                    "spot_balances":[{"currency":"USDT","balance":str(self.balance)}]}})
            if url.endswith("/open_orders"): return self.response(url,{"result":[]})
            if url == ORDER_URL:
                order = body["order"]; leg = order["legs"][0]
                cid = order["metadata"]["client_order_id"]
                assert cid not in self.venue_orders
                signed = Decimal(leg["size"]) * (1 if leg["is_buying_asset"] else -1)
                pnl = Decimal(0)
                if order["reduce_only"]:
                    assert signed*self.position < 0 and abs(signed) <= abs(self.position)
                    pnl = (Decimal(leg["limit_price"])-self.entry_price)*(-signed)
                else:
                    assert self.position == 0
                    self.entry_price = Decimal(leg["limit_price"])
                self.position += signed
                fee = Decimal("0.01")
                self.balance += pnl-fee
                stamp = time.time_ns()
                venue_order = {**order,"order_id":"0xloop"+str(len(self.venue_orders)+1),
                    "metadata":{**order["metadata"],"create_time":str(stamp)},
                    "state":{"status":"FILLED","update_time":str(stamp),"traded_size":[leg["size"]]}}
                self.venue_orders[cid] = venue_order
                self.orders.append({"id":cid,"side":"BUY" if signed > 0 else "SELL","reduce_only":order["reduce_only"],"chain_id":"326"})
                self.fills.append({"sub_account_id":sub,"order_id":venue_order["order_id"],"client_order_id":cid,
                    "instrument":"BTC_USDT_Perp","trade_id":"fill-"+str(len(self.fills)+1),"event_time":str(stamp),
                    "is_buyer":leg["is_buying_asset"],"is_taker":True,"is_liquidation":False,
                    "size":leg["size"],"price":leg["limit_price"],"fee":"0.01","fee_currency":"USDT",
                    "realized_pnl":str(pnl),"builder_fee":"0"})
                if order["reduce_only"] and self.funding_on_close:
                    self.balance -= self.funding_on_close
                    self.funding_rows.append({"sub_account_id":sub,"tx_id":self.fills[-1]["trade_id"],
                        "event_time":str(stamp),"instrument":"BTC_USDT_Perp","currency":"USDT",
                        "amount":str(self.funding_on_close)})
                # Acuse respeta exactamente los campos firmados; no inventa ejecución.
                return self.response(url,{"result":{**order,"order_id":venue_order["order_id"]}})
            if url == LOOKUP_URL: return self.response(url,{"result":self.venue_orders[body["client_order_id"]]})
            if url == FILL_HISTORY_URL:
                return self.response(url,{"result":[row for row in self.fills
                    if int(body["start_time"]) < int(row["event_time"]) <= int(body.get("end_time",time.time_ns()))],"next":""})
            if url == FUNDING_HISTORY_URL:
                return self.response(url,{"result":[row for row in self.funding_rows
                    if int(body["start_time"]) < int(row["event_time"]) <= int(body["end_time"])],"next":""})
            assert url in (FUNDING_HISTORY_URL,TRANSFER_HISTORY_URL,WITHDRAWAL_HISTORY_URL,DEPOSIT_HISTORY_URL,INTEREST_HISTORY_URL)
            return self.response(url,{"result":[],"next":""})
        return GrvtTestnetOrderClient(key,http_post=post)

    def close_source(self,opening):
        long = opening["direction"] == "LONG"
        mark = Decimal("110") if long else Decimal("80")
        assert self.observe_risk_mark(opening['session_id'],mark) == 'EXECUTED'
        with self.paper.connect() as db:
            action = db.execute("SELECT request_id FROM paper_signal_actions WHERE session_id=? AND action='CLOSE'",
                                (opening["session_id"],)).fetchone()
        result = {**opening,"closing":True,"action_request_id":action["request_id"]}
        result["client_order_id"] = client_id_for_signal(opening["session_id"],action["request_id"])
        self.price = Decimal("102") if long else Decimal("98")
        return result
