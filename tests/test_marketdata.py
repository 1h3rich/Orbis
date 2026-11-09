"""MarketData TERMINAL: adapters públicos (sin CCXT), normalización y servicio.

Todo con dobles de red (la suite debe ser verde sin internet): normalización
canónica, mapping nativo por tabla, ticker/OHLC parsing por adapter, errores
honestos (no disponible con motivo, nunca inventar), selección AUTO sin
mezclar silencioso, caché TTL y endpoints readonly. Integración separada con
TestClient (endpoints + WS + persistencia UI).
"""

from decimal import Decimal

import pytest

from app.marketdata import cache as _cache
from app.marketdata import service as svc
from app.marketdata.normalize import (
    alternatives,
    canonical_quote,
    canonical_symbol,
    native_symbol,
    parse_symbol,
)
from app.marketdata.registry import available_providers, get_provider, resolve_provider
from app.marketdata.stream import backoff_delays


@pytest.fixture(autouse=True)
def _clean():
    _cache.clear()
    yield
    _cache.clear()


# -- normalización --------------------------------------------------------


def test_canonical_upper_and_xbt_alias():
    assert canonical_symbol("btc", "eur") == "BTC/EUR"
    assert canonical_symbol("xbt", "eur") == "BTC/EUR"
    assert parse_symbol("BTC/EUR") == ("BTC", "EUR")
    assert parse_symbol("BTC-EUR") == ("BTC", "EUR")
    # Sin fusionar: EUR/USDT/USDC son mercados distintos.
    assert canonical_quote("usdt") == "USDT"
    assert canonical_quote("eur") == "EUR"
    assert canonical_quote("usdt") != canonical_quote("eur")


def test_native_mapping_per_adapter_table():
    assert native_symbol("kraken", "BTC", "EUR") == "XXBTZEUR"
    assert native_symbol("binance", "BTC", "USDT") == "BTCUSDT"
    assert native_symbol("coinbase", "BTC", "USDT") == "BTC-USDT"
    assert native_symbol("gate", "BTC", "USDT") == "BTC_USDT"
    assert native_symbol("grvt", "BTC", "USDT:PERP") == "BTC_USDT_Perp"
    with pytest.raises(ValueError, match="explícito"):
        native_symbol("grvt", "BTC", "USDT")


def test_alternatives_honest_message():
    alt = alternatives("BTC", "kraken", ["BTC/USDT"], quote="EUR")
    assert alt["available"] == ["BTC/USDT"]
    assert "BTC/EUR no disponible en kraken" in alt["message"]


def test_providers_registered():
    assert available_providers() == [
        "kraken", "binance", "coinbase", "okx", "bybit",
        "mexc", "kucoin", "gate", "bitget", "grvt", "bitstamp",
    ]


# -- adapters con mocks ----------------------------------------------------


def _kraken_ok(url, timeout=5.0):
    if "OHLC" in url:
        return {"error": [], "result": {"XXBTZEUR": [
            [1700000000, "77000", "77100", "76900", "77050", "1.0", 10],
            [1700000060, "77050", "77200", "77000", "77150", "2.0", 12],
        ], "last": 1700000060}}
    return {"error": [], "result": {"XXBTZEUR": {"c": ["77317.50000", "0.01"]}}}


def _binance_ok(url, timeout=5.0):
    if "klines" in url:
        return [[1700000000000, "77000", "77100", "76900", "77050", "1.5"],
                [1700000060000, "77050", "77200", "77000", "77150", "2.5"]]
    return {"lastPrice": "77050", "bidPrice": "77049", "askPrice": "77051",
            "highPrice": "78000", "lowPrice": "76000", "priceChangePercent": "1.2",
            "volume": "10", "quoteVolume": "770500"}


def _fails(url, timeout=5.0):
    raise ConnectionError("red caída")


def test_kraken_ticker_and_candles():
    prov = get_provider("kraken", http_get=_kraken_ok)
    ticker = prov.get_ticker("BTC/EUR")
    assert ticker["price"] == "77317.50000"
    assert prov.get_price("BTC/EUR") == Decimal("77317.50000")
    candles = prov.get_candles("BTC/EUR", timeframe="1h")
    assert [c["c"] for c in candles] == ["77050", "77150"]
    assert prov.health()["status"] in ("AVAILABLE", "DEGRADED")


def test_candle_service_marks_current_kraken_bar_open(monkeypatch):
    monkeypatch.setattr(svc.time, "time", lambda: 1_700_000_060)
    result = svc.candles("BTC/EUR", provider="kraken", timeframe="1m", limit=2,
                         http_get=_kraken_ok)
    assert result["available"] is True
    assert [bar["closed"] for bar in result["candles"]] == [True, False]
    assert [bar["t"] for bar in result["candles"]] == [1_700_000_000, 1_700_000_060]


def test_monthly_candle_end_uses_calendar_not_thirty_days():
    from datetime import datetime, timezone

    start = int(datetime(2024, 2, 1, tzinfo=timezone.utc).timestamp())
    march = int(datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp())
    assert svc._candle_end(start, "1M") == march
    assert svc._candle_end(start, "1M") - start == 29 * 86400


def test_candle_service_never_promotes_provider_open_or_future_bar(monkeypatch):
    class Provider:
        timeframes = ("1m",)

        def get_candles(self, symbol, timeframe, limit, since):
            return [{"t": 600, "o": "1", "h": "2", "l": "1", "c": "2", "closed": False},
                    {"t": 900, "o": "2", "h": "3", "l": "2", "c": "3", "closed": True}]

    monkeypatch.setattr(svc.time, "time", lambda: 900)
    monkeypatch.setattr(svc, "resolve_provider", lambda provider, symbol, http_get: (Provider(), "fake"))
    result = svc.candles("BTC/EUR", provider="fake", timeframe="1m", limit=2,
                         http_get=lambda *args: None)
    assert [bar["closed"] for bar in result["candles"]] == [False, False]


def test_binance_ticker_and_candles_with_bid_ask():
    prov = get_provider("binance", http_get=_binance_ok)
    ticker = prov.get_ticker("BTC/USDT")
    assert ticker["price"] == "77050"
    assert ticker["bid"] == "77049" and ticker["ask"] == "77051"
    candles = prov.get_candles("BTC/USDT", timeframe="1h")
    assert len(candles) == 2 and candles[0]["t"] == 1700000000


def test_mexc_ticker_rejects_wrong_or_missing_native_symbol():
    from app.marketdata.adapters.mexc import MexcAdapter

    row = {"symbol": "ETHUSDT", "lastPrice": "70000", "bidPrice": "69999",
           "askPrice": "70001"}
    adapter = MexcAdapter(http_get=lambda url: row)
    assert adapter.get_ticker("BTC/USDT") is None
    row.pop("symbol")
    assert adapter.get_ticker("BTC/USDT") is None
    row["symbol"] = "BTCUSDT"
    ticker = adapter.get_ticker("BTC/USDT")
    assert ticker is not None
    assert ticker["symbol"] == "BTC/USDT" and ticker["price"] == "70000"


def test_unknown_pair_returns_none_with_reason():
    prov = get_provider("binance", http_get=_binance_ok)
    assert prov.get_ticker("XXX/EUR") is None
    out = svc.snapshot("ADA/EUR", provider="binance", http_get=_binance_ok)
    assert out["available"] is False
    assert "no disponible en binance" in out["reason"]
    assert out["actions"] == ["Reintentar", "Cambiar proveedor"]


def test_snapshot_rejects_wrong_adapter_symbol_without_caching(monkeypatch):
    class Provider:
        returned = "ETH/EUR"

        def get_ticker(self, symbol):
            return {"symbol": self.returned, "price": "50000", "source": "kraken"}

    provider = Provider()
    monkeypatch.setattr(svc, "resolve_provider", lambda *args, **kwargs: (provider, "kraken"))
    wrong = svc.snapshot("BTC/EUR", provider="kraken")
    assert wrong["available"] is False
    assert wrong["price"] is None
    assert "otro mercado" in wrong["reason"]
    provider.returned = "BTC/EUR"
    correct = svc.snapshot("BTC/EUR", provider="kraken")
    assert correct["available"] is True and correct["price"] == "50000"
    _cache.clear()
    provider.returned = None
    missing = svc.snapshot("BTC/EUR", provider="kraken")
    assert missing["available"] is False


def test_errors_never_raise_and_health_honest():
    prov = get_provider("kraken", http_get=_fails)
    assert prov.get_ticker("BTC/EUR") is None
    assert prov.get_price("BTC/EUR") is None
    assert prov.get_candles("BTC/EUR") is None
    assert prov.health()["status"] == "DEGRADED"


def test_grvt_perpetual_ticker_is_distinct_from_spot():
    from app.marketdata.adapters.grvt import GrvtAdapter

    calls = []

    def post(url, body):
        calls.append((url, body))
        return {"result": {
            "instrument": "BTC_USDT_Perp", "last_price": "65038.01",
            "event_time": "1700000000000000000",
            "mark_price": "65035.00", "best_bid_price": "65037.00",
            "best_ask_price": "65039.00",
        }}

    prov = GrvtAdapter(http_post=post)
    assert prov.get_ticker("BTC/USDT") is None
    assert calls == []
    ticker = prov.get_ticker("BTC/USDT:PERP")
    assert calls == [(
        "https://market-data.grvt.io/full/v1/ticker",
        {"instrument": "BTC_USDT_Perp"},
    )]
    assert ticker["price"] == "65038.01"
    assert ticker["mark_price"] == "65035.00"
    assert ticker["event_time_ns"] == 1700000000000000000
    assert ticker["instrument_type"] == "PERPETUAL"
    assert prov.ws_kind == "none"
    assert "REST puntual" in prov.health()["detail_human"]
    assert "AUTO PAPER usa un canal público independiente" in prov.health()["detail_human"]


@pytest.mark.parametrize("event_time", [1700000000000000000, 1700000000000000000.0,
                                        True, "1700000000000000000.5",
                                        " 1700000000000000000", "-1700000000000000000"])
def test_grvt_rest_ticker_requires_exact_textual_event_time(event_time):
    from app.marketdata.adapters.grvt import GrvtAdapter

    prov = GrvtAdapter(http_post=lambda _url, _body: {"result": {
        "instrument": "BTC_USDT_Perp", "last_price": "65038.01",
        "event_time": event_time, "mark_price": "65035.00",
        "best_bid_price": "65037.00", "best_ask_price": "65039.00",
    }})
    assert prov.get_ticker("BTC/USDT:PERP") is None
    assert prov.health()["status"] == "DEGRADED"


def test_grvt_candles_use_post_nanoseconds_and_normalize_order(monkeypatch):
    from app.marketdata.adapters.grvt import GrvtAdapter
    import app.marketdata.adapters.grvt as grvt_module

    monkeypatch.setattr(grvt_module.time, "time_ns", lambda: 1_800_000_000_000_000_000)
    calls = []

    def post(url, body):
        calls.append((url, body))
        return {"result": [
            {"instrument": "BTC_USDT_Perp", "open_time": "1700003600000000000",
             "close_time": "1700004000000000000",
             "open": "2", "high": "3", "low": "1", "close": "2.5", "volume_b": "4"},
            {"instrument": "BTC_USDT_Perp", "open_time": "1700000000000000000",
             "close_time": "1700003600000000000",
             "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume_b": "3"},
        ]}

    prov = GrvtAdapter(http_post=post)
    rows = prov.get_candles("BTC/USDT:PERP", timeframe="1h", limit=2, since=1700000000)
    assert [row["t"] for row in rows] == [1700000000, 1700003600]
    assert rows[1]["c"] == "2.5" and rows[1]["v"] == "4"
    assert rows[0]["closed"] is True and rows[1]["closed"] is False
    assert calls == [(
        "https://market-data.grvt.io/full/v1/kline",
        {"instrument": "BTC_USDT_Perp", "interval": "CI_1_H", "type": "TRADE",
         "limit": 2, "start_time": "1700000000000000000",
         "end_time": "1700007199999999999"},
    )]


def test_grvt_rejects_mismatched_instrument_and_reports_degraded():
    from app.marketdata.adapters.grvt import GrvtAdapter

    prov = GrvtAdapter(http_post=lambda url, body: {"result": [
        {"instrument": "ETH_USDT_Perp", "open_time": "1700000000000000000",
         "close_time": "1700003600000000000",
         "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume_b": "3"},
    ]})
    assert prov.get_candles("BTC/USDT:PERP") is None
    assert prov.health()["status"] == "DEGRADED"


def test_grvt_margin_rules_are_readonly_and_tied_to_instrument():
    from app.marketdata.adapters.grvt import GrvtAdapter

    calls = []

    def post(url, body):
        calls.append((url, body))
        return {"instrument": "BTC_USDT_Perp", "max_position_size": "1000.0",
                "risk_brackets": [{"tier": 1, "notional_floor": "0.0",
                                   "notional_cap": "600000.0", "maintenance_margin_rate": "0.01",
                                   "initial_margin_rate": "0.02", "max_leverage": 50,
                                   "cumulative_maintenance_amount": "0.0"}]}

    prov = GrvtAdapter(http_post=post)
    assert prov.get_margin_rules("BTC/USDT") is None
    rules = prov.get_margin_rules("BTC/USDT:PERP")
    assert rules["instrument"] == "BTC_USDT_Perp"
    assert rules["risk_brackets"][0]["maintenance_margin_rate"] == "0.01"
    assert isinstance(rules["fetched_at_ns"], int)
    assert calls == [("https://market-data.grvt.io/full/v1/margin_rules",
                      {"instrument": "BTC_USDT_Perp"})]


def test_grvt_instrument_spec_uses_public_endpoint_and_never_spot():
    from app.marketdata.adapters.grvt import GrvtAdapter

    calls = []

    def post(url, body):
        calls.append((url, body))
        return {"result": {"instrument": "BTC_USDT_Perp", "kind": "PERPETUAL",
                           "venues": ["ORDERBOOK", "RFQ"], "base_decimals": 9,
                           "tick_size": "0.1", "min_size": "0.001",
                           "min_notional": "100.0", "max_position_size": "1000.0"}}

    adapter = GrvtAdapter(http_post=post)
    assert adapter.get_instrument_spec("BTC/USDT") is None
    spec = adapter.get_instrument_spec("BTC/USDT:PERP")
    assert spec["min_size"] == "0.001" and spec["base_decimals"] == 9
    assert isinstance(spec["fetched_at_ns"], int)
    assert calls == [("https://market-data.grvt.io/full/v1/instrument",
                      {"instrument": "BTC_USDT_Perp"})]
    rejected = GrvtAdapter(http_post=lambda _url, _body: {"result": {
        "instrument": "BTC_USDT_Perp", "kind": "FUTURE", "venues": ["ORDERBOOK"]}})
    assert rejected.get_instrument_spec("BTC/USDT:PERP") is None


def test_grvt_service_does_not_auto_mix_perpetual_with_spot(monkeypatch):
    from app.marketdata.adapters.grvt import GrvtAdapter
    import app.marketdata.registry as registry

    prov = GrvtAdapter(http_post=lambda url, body: {"result": {
        "instrument": "BTC_USDT_Perp", "last_price": "65038.01",
        "event_time": "1700000000000000000",
        "mark_price": "65035.00",
    }})
    monkeypatch.setitem(registry._instances, "grvt", prov)
    spot = svc.snapshot("BTC/USDT", provider="grvt")
    assert spot["available"] is False
    perp = svc.snapshot("BTC/USDT:PERP", provider="grvt")
    assert perp["available"] is True
    assert perp["instrument_type"] == "PERPETUAL"
    assert perp["mark_price"] == "65035.00"


def test_spot_paper_api_blocks_grvt_perpetual_before_ledger_write(client):
    from uuid import uuid4

    buy = client.post("/paper/buy", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "USDT",
        "order_amount": "100", "price": "65000", "exchange": "grvt",
    })
    sell = client.post("/paper/sell", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "USDT",
        "quantity": "0.001", "price": "65000", "exchange": "grvt",
    })
    assert buy.status_code == 422 and sell.status_code == 422
    assert client.get("/paper/operations").json() == []


def test_candles_dedupe_and_timeframe_gate():
    out = svc.candles("BTC/EUR", provider="kraken", timeframe="1h", http_get=_kraken_ok)
    assert out["available"] is True
    times = [c["t"] for c in out["candles"]]
    assert times == sorted(times) and len(set(times)) == len(times)
    bad = svc.candles("BTC/EUR", provider="kraken", timeframe="3m", http_get=_kraken_ok)
    assert bad["available"] is False and "no nativo" in bad["reason"]


def test_auto_resolution_is_visible_not_silent():
    out = svc.snapshot("BTC/EUR", provider="auto", http_get=_kraken_ok)
    assert out["available"] is True
    assert out["resolved_provider"] == "kraken"
    assert out["requested_provider"] == "auto"
    assert isinstance(out["price"], str)


def test_auto_candles_resolve_pair_and_native_timeframe_without_changing_explicit_source():
    weekly, weekly_name = resolve_provider("auto", symbol="BTC/EUR",
                                            timeframe="1w", http_get=_kraken_ok)
    monthly, monthly_name = resolve_provider("auto", symbol="BTC/EUR",
                                              timeframe="1M", http_get=_binance_ok)
    missing, missing_name = resolve_provider("auto", symbol="BTC/EUR",
                                            timeframe="3m", http_get=_kraken_ok)
    explicit, explicit_name = resolve_provider("kraken", symbol="BTC/EUR",
                                                timeframe="1M", http_get=_kraken_ok)
    assert weekly_name == "kraken" and "1w" in weekly.timeframes
    assert monthly_name == "binance" and "1M" in monthly.timeframes
    assert missing is None and missing_name == "auto"
    assert explicit_name == "kraken" and "1M" not in explicit.timeframes
    monthly_page = svc.candles(
        "BTC/EUR", provider="auto", timeframe="1M", http_get=lambda url, timeout=5.0: [
            [1704067200000, "100", "110", "90", "105", "2"]
        ] if "klines" in url else {})
    assert monthly_page["available"] is True
    assert monthly_page["resolved_provider"] == "binance"
    assert monthly_page["candles"][0]["t"] == 1704067200
    unavailable = svc.candles("BTC/EUR", provider="auto", timeframe="3m",
                              http_get=_kraken_ok)
    assert unavailable["available"] is False
    assert "BTC/EUR/3m" in unavailable["reason"]


def test_snapshot_separates_market_time_from_local_receipt_and_ages_cached_data(monkeypatch):
    class Provider:
        def get_ticker(self, symbol):
            return {"symbol": symbol, "price": "100", "source": "grvt",
                    "event_time_ns": 950_000_000_000}

    monkeypatch.setattr(svc, "resolve_provider", lambda *args, **kwargs: (Provider(), "grvt"))
    now = {"seconds": 1000}
    monkeypatch.setattr(svc.time, "time", lambda: now["seconds"])
    fresh = svc.snapshot("BTC/USDT:PERP", provider="grvt")
    assert fresh["market_time_status"] == "fresh"
    assert fresh["market_age_seconds"] == 50
    assert fresh["market_time_at"] != fresh["received_at"]

    now["seconds"] = 1020
    cached = svc.snapshot("BTC/USDT:PERP", provider="grvt")
    assert cached["cached"] is True
    assert cached["received_at"] == fresh["received_at"]
    assert cached["market_time_status"] == "stale"
    assert cached["market_age_seconds"] == 70


def test_snapshot_without_provider_event_time_does_not_claim_market_freshness(monkeypatch):
    class Provider:
        def get_ticker(self, symbol):
            return {"symbol": symbol, "price": "100", "source": "kraken",
                    "as_of": "2026-10-23T00:00:00Z"}

    monkeypatch.setattr(svc, "resolve_provider", lambda *args, **kwargs: (Provider(), "kraken"))
    result = svc.snapshot("BTC/EUR", provider="kraken", http_get=lambda *_: None)
    assert result["available"] is True
    assert result["received_at"]
    assert result["market_time_at"] is None
    assert result["market_time_status"] == "unknown"
    assert result["market_age_seconds"] is None


def test_backoff_sequence():
    assert backoff_delays(6)[:4] == [1, 2, 4, 8]
    assert max(backoff_delays(12)) <= 30


# -- validación financiera Decimal (F5) ------------------------------------


def test_financial_validation_weighted_average_and_sale():
    from app.math.positions import build_positions

    class Op:
        def __init__(self, i, typ, spent, received, price):
            self.id = i
            self.timestamp = None
            self.operation_type = typ
            self.mode = "PAPER"
            self.status = "EXECUTED"
            self.asset = "BTC"
            self.quote_currency = "EUR"
            self.amount_spent = Decimal(spent)
            self.asset_received = Decimal(received)
            self.price = Decimal(price)
            self.trading_fee = self.withdrawal_fee = self.network_fee = Decimal("0")

    # 100€@50k => 0.002/100/50k
    ops = [Op(1, "BUY", "100", "0.002", "50000")]
    [p] = build_positions(ops)
    assert p.quantity == Decimal("0.002") and p.invested == Decimal("100")
    assert p.average_cost == Decimal("50000")
    # +100€@100k => 0.003/200/66666.6666...
    ops.append(Op(2, "BUY", "100", "0.001", "100000"))
    [p] = build_positions(ops)
    assert p.quantity == Decimal("0.003") and p.invested == Decimal("200")
    assert abs(p.average_cost - Decimal("200") / Decimal("0.003")) < Decimal("0.01")
    # venta 0.001@70k => realized 3.333..., invested 133.333..., qty 0.002, avg no cambia
    avg_before = p.average_cost
    ops.append(Op(3, "SELL", "70", "0.001", "70000"))
    [p] = build_positions(ops)
    assert p.quantity == Decimal("0.002")
    assert p.average_cost == avg_before
    assert abs(p.realized_pnl - (Decimal("70000") - avg_before) * Decimal("0.001")) < Decimal("0.000001")
    assert abs(p.invested - (Decimal("200") - avg_before * Decimal("0.001"))) < Decimal("0.000001")
