"""UI real con respuestas MEXC controladas; nunca utiliza la red del exchange."""

import os
import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI

from app.portfolio.api import create_real_router
from app.web.mount import register_ui


class FakeMexc:
    def __init__(self, api_key, secret_key):
        if (api_key, secret_key) != ("browser-test-key", "browser-test-secret"):
            raise ValueError("Credencial de prueba inválida")

    def balances(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "balances": [{"asset": "BTC", "free": "0.25", "locked": "0.01", "total": "0.26"},
                             {"asset": "USDT", "free": "100", "locked": "0", "total": "100"}]}

    def account_uid(self):
        return "209302839"

    def account_reference(self):
        return hmac.new(b"browser-test-secret", b"browser-test-key", hashlib.sha256).hexdigest()

    def ethereum_erc20_deposit_destination(self, asset):
        assert asset == "USDT"
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "network": "ETHEREUM_MAINNET", "asset": asset,
                "token_contract": "0x" + "44" * 20,
                "deposit_address": "0x" + "33" * 20,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "stored": False, "transfer_sent": False, "ready_for_execution": False}
    def open_orders_unfiltered(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "UNFILTERED_COVERAGE_UNPROVEN", "symbol": None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "orders": [{"symbol": "ETHUSDT", "status": "NEW",
                            "order_id": "browser-external-order",
                            "client_order_id": "browser-external-client", "side": "BUY",
                            "type": "LIMIT", "price": "100", "quantity": "0.5",
                            "executed_quantity": "0", "remaining_quantity": "0.5"}]}

    def trades(self, symbol):
        assert symbol == "BTCUSDT"
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_MONTH_SINGLE_PAGE_INCOMPLETE", "symbol": symbol,
                "limit": 100, "observed_at": "2026-10-13T00:00:00+00:00",
                "trades": [{"id": "browser-fill-1", "order_id": "browser-order-1",
                            "side": "BUY", "time_ms": 1_800_000_000_000,
                            "price": "100.50", "quantity": "0.25", "quote_quantity": "25.125",
                            "commission": "0.001", "commission_asset": "USDT"}]}

    def trades_window(self, symbol, *, days):
        assert days in (1, 7, 30)
        page = self.trades(symbol)
        return {**page, "coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT",
                "limit": 1000, "start_ms": 1_799_000_000_000,
                "end_ms": 1_801_000_000_000, "pages": 1}

    def transfers_recent(self, *, days, include_source_ids=False):
        assert days in (1, 7, 30, 90)
        assert include_source_ids is True
        end_ms = 1_800_000_000_000
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT", "start_ms": end_ms - days * 86_400_000 + (60_000 if days == 90 else 0),
                "end_ms": end_ms, "requests": 26 if days == 90 else 10 if days == 30 else 2,
                "observed_at": "2026-10-13T00:00:00+00:00",
                "deposits": [{"direction": "deposit", "asset": "BTC", "network": "BTC",
                              "amount": "0.125", "fee": "0", "status": 5,
                              "time_ms": 1_799_999_999_000}], "withdrawals": []}


class FakeMexcTrade:
    def __init__(self, api_key, secret_key):
        if (api_key, secret_key) not in (("browser-trade-key", "browser-trade-secret"),
                                         ("browser-other-trade-key", "browser-trade-secret")):
            raise ValueError("Credencial Trade de prueba inválida")
        self._same = api_key == "browser-trade-key"

    def account_uid(self):
        return "209302839" if self._same else "99887766"


class FakeGrvt:
    def __init__(self, api_key):
        if api_key != "browser-grvt-view-key":
            raise ValueError("Credencial de prueba inválida")

    def snapshot(self):
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "permission_verified": False, "observed_at": "2026-10-13T00:00:00+00:00",
                "total_equity_usd": "110.01", "trading_equity_usd": "100.01",
                "spot_balances_aggregated": [{"asset": "USDT", "balance": "10"}],
                "trading_accounts": [{"sub_account_id": "123456", "settle_currency": "USDT",
                                      "total_equity": "100.01", "maintenance_margin": "1.5",
                                      "unrealized_pnl": "2.5", "balances": [],
                                      "event_time_ns": "1800000000000000000", "fresh": True,
                                      "positions": [{"instrument": "BTC_USDT_Perp", "side": "LONG",
                                                     "size": "0.25", "entry_price": "100",
                                                     "mark_price": "110", "unrealized_pnl": "2.5"}]}]}

    def open_orders(self):
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": "2026-10-13T00:00:00+00:00",
                "in_flight_orders_excluded_possible": True, "complete_for_execution": False,
                "orders": [{"sub_account_id": "123456", "order_id": "0x123",
                            "client_order_id": "9223372036854775808", "status": "OPEN",
                            "reduce_only": True, "update_time_ns": "1800000000000000000",
                            "legs": [{"instrument": "BTC_USDT_Perp", "side": "SELL",
                                      "size": "0.3", "traded_size": "0.1",
                                      "limit_price": "65000"}]}]}

    def order(self, sub_account_id, client_order_id):
        assert (sub_account_id, client_order_id) == ("123456", "9223372036854775808")
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": "2026-10-13T00:00:00+00:00", "complete_for_execution": False,
                "order": {"sub_account_id": sub_account_id, "client_order_id": client_order_id,
                          "order_id": "0x123", "status": "FILLED", "reduce_only": True,
                          "update_time_ns": "1800000000000000000",
                          "legs": [{"instrument": "BTC_USDT_Perp", "side": "SELL",
                                    "size": "0.3", "traded_size": "0.3",
                                    "limit_price": "65000"}]}}

    def order_fills(self, sub_account_id, client_order_id):
        assert (sub_account_id, client_order_id) == ("123456", "9223372036854775808")
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": "2026-10-13T00:00:00+00:00",
                "account_ref": "a" * 64,
                "history_pages_drained": True, "fill_sizes_match_order_state": True,
                "complete_for_execution": False,
                "order": {"sub_account_id": sub_account_id, "client_order_id": client_order_id,
                          "order_id": "0x123", "status": "FILLED", "reduce_only": True,
                          "create_time_ns": "1790000000000000000",
                          "update_time_ns": "1790000000000000001",
                          "legs": [{"instrument": "BTC_USDT_Perp", "side": "SELL",
                                    "size": "0.3", "traded_size": "0.3", "limit_price": "65000"}]},
                "fills": [{"trade_id": "trade-1", "event_time_ns": "1790000000000000000",
                           "instrument": "BTC_USDT_Perp", "side": "SELL", "role": "TAKER",
                           "size": "0.3", "price": "65000", "fee": "0.12",
                           "builder_fee": "0", "fee_currency": "USDT", "realized_pnl": "1.5"}]}


def fake_mexc_quote(symbol):
    if symbol != "BTCUSDT":
        raise ValueError("Cotización no disponible")
    return {"symbol": symbol, "closeTime": int(datetime.now(timezone.utc).timestamp() * 1000),
            "bidPrice": "100000", "askPrice": "100001", "lastPrice": "100000"}


app = FastAPI()
app.include_router(create_real_router(Path(os.environ["ORBIS_REAL_VAULT_PATH"]),
                                      mexc_factory=FakeMexc, mexc_trade_factory=FakeMexcTrade,
                                      grvt_factory=FakeGrvt,
                                      mexc_quote_get=fake_mexc_quote))
register_ui(app)
