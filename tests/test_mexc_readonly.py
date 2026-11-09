"""MEXC privado: lectura estricta, sin usar claves reales ni red."""

from decimal import Decimal, localcontext
import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError


KEY = "test-access-key-only"
SECRET = "test-secret-for-fixtures"


def source(*, permissions="SPOT_ACCOUNT_READ,SPOT_DEAL_READ", balances=None, trades=None,
           uid="209302839", order=None, order_trades=None, open_orders=None,
           open_symbol="BTCUSDT", mx_response=None):
    calls = []

    def fake_get(url, headers):
        parsed = urlparse(url)
        assert parsed.scheme == "https" and parsed.netloc == "api.mexc.com"
        assert parsed.path in ("/api/v3/apiKeyInfo", "/api/v3/account", "/api/v3/myTrades",
                               "/api/v3/uid", "/api/v3/order", "/api/v3/openOrders", "/api/v3/mxDeduct/enable")
        assert headers == {"X-MEXC-APIKEY": KEY}
        unsigned, signature = parsed.query.rsplit("&signature=", 1)
        assert signature == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        assert len(signature) == 64
        assert parse_qs(unsigned)["timestamp"] == ["1800000000000"]
        assert parse_qs(unsigned)["recvWindow"] == ["5000"]
        calls.append(parsed.path)
        if parsed.path.endswith("apiKeyInfo"):
            assert parse_qs(unsigned)["accessKey"] == [KEY]
            return {"status": "VALID", "permissions": permissions, "apikey": KEY,
                    "remainingValidity": 30}
        if parsed.path.endswith("/uid"):
            return {"uid": uid}
        if parsed.path.endswith("/mxDeduct/enable"):
            assert set(parse_qs(unsigned)) == {"timestamp", "recvWindow"}
            return mx_response
        if parsed.path.endswith("/openOrders"):
            assert parse_qs(unsigned).get("symbol") == ([open_symbol] if open_symbol else None)
            return open_orders if open_orders is not None else []
        if parsed.path.endswith("/order"):
            assert parse_qs(unsigned)["symbol"] == ["BTCUSDT"]
            assert parse_qs(unsigned)["origClientOrderId"] == ["a" * 32]
            return order if order is not None else {
                "symbol": "BTCUSDT", "clientOrderId": "a" * 32,
                "orderId": "order-1",
                "price": "65000.00", "Qty": "0.01", "executedQty": "0.002",
                "cumulativeQuoteQty": "130", "status": "PARTIALLY_FILLED",
                "type": "LIMIT", "side": "BUY",
            }
        if parsed.path.endswith("myTrades"):
            assert parse_qs(unsigned)["symbol"] == ["BTCUSDT"]
            if "orderId" in parse_qs(unsigned):
                assert parse_qs(unsigned)["orderId"] == ["order-1"]
                assert parse_qs(unsigned)["limit"] == ["1000"]
                return order_trades if order_trades is not None else [{
                    "symbol": "BTCUSDT", "id": "fill-1", "orderId": "order-1",
                    "price": "65000", "qty": "0.002", "quoteQty": "130",
                    "commission": "0.01", "commissionAsset": "USDT",
                    "time": 1_799_999_999_000, "isBuyer": True,
                }]
            assert parse_qs(unsigned)["limit"] == ["100"]
            return trades if trades is not None else [{
                "symbol": "BTCUSDT", "id": "fill-1", "orderId": "order-1",
                "price": "65000.12345678", "qty": "0.000000000000000123",
                "quoteQty": "0.00000000799590777778", "commission": "0.00000001",
                "commissionAsset": "BNB", "time": 1_799_999_999_000,
                "isBuyer": True,
            }]
        return {"accountType": "SPOT", "balances": balances if balances is not None else [
            {"asset": "BTC", "free": "0.123456789012345678", "locked": "0.000000000000000022"},
            {"asset": "USDT", "free": "10.00", "locked": "0"},
            {"asset": "ETH", "free": "0", "locked": "0"},
        ]}

    return fake_get, calls


def test_signed_readonly_balances_are_exact_and_separate_from_portfolio():
    fake_get, calls = source()
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    assert "test-access" not in repr(client) and "test-secret" not in repr(client)
    result = client.balances()
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/account"]
    assert result["accounting"] == "OBSERVED_NOT_IMPORTED"
    assert result["source"] == "MEXC_SPOT_PRIVATE_READONLY"
    assert result["balances"] == [
        {"asset": "BTC", "free": "0.123456789012345678", "locked": "0.000000000000000022",
         "total": "0.123456789012345700"},
        {"asset": "USDT", "free": "10.00", "locked": "0", "total": "10.00"},
    ]
    assert Decimal(result["balances"][0]["total"]) == Decimal("0.123456789012345700")
    assert all("key" not in str(row).lower() for row in result["balances"])


@pytest.mark.parametrize("enabled", [False, True])
def test_mx_fee_preference_uses_only_the_signed_read_endpoint(enabled):
    fake_get, calls = source(mx_response={"code": 0, "msg": "success", "data": {"mxDeductEnable": enabled}})
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get, clock_ms=lambda: 1_800_000_000_000)
    assert client.mx_deduct_enabled() is enabled
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/mxDeduct/enable"]


@pytest.mark.parametrize("payload", [
    {"code": 401, "data": {"mxDeductEnable": False}},
    {"code": False, "data": {"mxDeductEnable": False}},
    {"code": 0, "data": {"mxDeductEnable": "false"}},
    {"code": 0, "data": {}},
])
def test_mx_fee_preference_requires_an_explicit_verified_boolean(payload):
    fake_get, _ = source(mx_response=payload)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get, clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError, match="comisiones en MX"):
        client.mx_deduct_enabled()


def test_balance_total_keeps_tiny_locked_units_or_rejects_unrepresentable_sum():
    fake_get, _ = source(balances=[{"asset": "BTC", "free": "0.001", "locked": "1e-39"}])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    row = client.balances()["balances"][0]
    with localcontext() as context:
        context.prec = 200
        assert Decimal(row["total"]) == Decimal("0.001") + Decimal("1e-39")
    assert Decimal(row["total"]) != Decimal("0.001")
    fake_get, _ = source(balances=[{"asset": "BTC", "free": "1", "locked": "1e-100"}])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    extreme = client.balances()["balances"][0]
    assert Decimal(extreme["total"]) != Decimal("1")
    fake_get, _ = source(balances=[{"asset": "BTC", "free": "1e100", "locked": "1e-100"}])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    extreme = client.balances()["balances"][0]
    with localcontext() as context:
        context.prec = 300
        assert Decimal(extreme["total"]) == Decimal("1e100") + Decimal("1e-100")


def _open_order(**changes):
    row = {"symbol": "BTCUSDT", "orderId": "order-1", "clientOrderId": "a" * 32,
           "side": "BUY", "type": "LIMIT", "status": "PARTIALLY_FILLED",
           "price": "65000.00", "origQty": "0.0100", "executedQty": "0.0020"}
    return row | changes


def test_open_orders_are_exact_signed_symbol_scoped_and_not_booked():
    fake_get, calls = source(open_orders=[_open_order()])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    result = client.open_orders("BTCUSDT")
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/openOrders"]
    assert result["accounting"] == "OBSERVED_NOT_IMPORTED"
    assert result["coverage"] == "ALL_OPEN_ORDERS_FOR_ONE_SYMBOL"
    assert result["orders"] == [{
        "symbol": "BTCUSDT", "order_id": "order-1", "client_order_id": "a" * 32,
        "side": "BUY", "type": "LIMIT", "status": "PARTIALLY_FILLED",
        "price": "65000.00", "quantity": "0.0100", "executed_quantity": "0.0020",
        "remaining_quantity": "0.0080",
    }]


def test_open_order_remaining_quantity_keeps_high_precision_or_fails_closed():
    original = "0.1234567890123456789012345678901234567890"
    executed = "0.0000000000000000000000000000000000000001"
    fake_get, _ = source(open_orders=[_open_order(origQty=original, executedQty=executed)])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    assert client.open_orders("BTCUSDT")["orders"][0]["remaining_quantity"] == (
        "0.1234567890123456789012345678901234567889")
    fake_get, _ = source(open_orders=[_open_order(origQty="1", executedQty="1e-100")])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError, match="importe"):
        client.open_orders("BTCUSDT")


@pytest.mark.parametrize("rows", [
    [_open_order(symbol="ETHUSDT")],
    [_open_order(status="FILLED")],
    [_open_order(executedQty="0.0100")],
    [_open_order(), _open_order(orderId="order-2")],
    [_open_order(), _open_order(clientOrderId="b" * 32)],
    [_open_order(price="NaN")],
    [_open_order(type="LIMIT", price="0")],
])
def test_open_orders_reject_incomplete_or_incoherent_risk_evidence(rows):
    fake_get, _ = source(open_orders=rows)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.open_orders("BTCUSDT")


def test_open_orders_requires_deal_read_permission_before_query():
    fake_get, calls = source(permissions="SPOT_ACCOUNT_READ")
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError, match="SPOT_DEAL_READ"):
        client.open_orders("BTCUSDT")
    assert calls == ["/api/v3/apiKeyInfo"]


def test_open_orders_full_page_is_not_claimed_complete():
    fake_get, _ = source(open_orders=[_open_order()] * 1000)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError, match="Cobertura"):
        client.open_orders("BTCUSDT")


def test_unfiltered_open_orders_discover_other_pairs_without_claiming_coverage():
    fake_get, calls = source(open_orders=[_open_order(),
                                          _open_order(symbol="ETHUSDT", orderId="order-2",
                                                      clientOrderId="b" * 32)],
                             open_symbol=None)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    snapshot = client.open_orders_unfiltered()
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/openOrders"]
    assert snapshot["coverage"] == "UNFILTERED_COVERAGE_UNPROVEN"
    assert snapshot["symbol"] is None
    assert [row["symbol"] for row in snapshot["orders"]] == ["BTCUSDT", "ETHUSDT"]
    assert snapshot["accounting"] == "OBSERVED_NOT_IMPORTED"


@pytest.mark.parametrize("rows", [
    [_open_order(), _open_order(symbol="ETHUSDT", orderId="order-1", clientOrderId="b" * 32)],
    [_open_order(symbol="bad/symbol")],
    [_open_order()] * 1000,
])
def test_unfiltered_open_orders_reject_incoherent_or_full_response(rows):
    fake_get, _ = source(open_orders=rows, open_symbol=None)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.open_orders_unfiltered()


def test_balance_sum_does_not_round_at_default_decimal_precision():
    fake_get, _ = source(balances=[{"asset": "BTC", "free": "9" * 40, "locked": "1"}])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    assert client.balances()["balances"][0]["total"] == "1" + "0" * 40


@pytest.mark.parametrize("permissions", [
    "SPOT_ACCOUNT_READ,SPOT_DEAL_WRITE",
    "SPOT_ACCOUNT_READ,SPOT_TRANSFER_WRITE",
    "SPOT_DEAL_READ",
    "SPOT_ACCOUNT_READ,UNKNOWN",
])
def test_rejects_non_readonly_or_missing_account_permission(permissions):
    fake_get, calls = source(permissions=permissions)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.balances()
    assert calls == ["/api/v3/apiKeyInfo"]


@pytest.mark.parametrize("balances", [
    [{"asset": "BTC", "free": "NaN", "locked": "0"}],
    [{"asset": "BTC", "free": "-1", "locked": "0"}],
    [{"asset": "BTC", "free": 1.1, "locked": "0"}],
    [{"asset": "BTC", "free": "1e100000", "locked": "0"}],
    [{"asset": "BTC", "free": "1", "locked": "0"},
     {"asset": "BTC", "free": "2", "locked": "0"}],
])
def test_rejects_invalid_financial_data(balances):
    fake_get, _ = source(balances=balances)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.balances()


def test_only_whitelisted_get_endpoints_are_possible():
    fake_get, calls = source()
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError, match="no permitida"):
        client._signed_get("/api/v3/order/test")
    assert calls == []


def test_account_uid_is_signed_readonly_and_never_in_balance_result():
    fake_get, calls = source()
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    assert client.account_uid() == "209302839"
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/uid"]


@pytest.mark.parametrize("uid", [None, 0, "", "0", "001", "abc", "1 2", "9" * 33])
def test_account_uid_rejects_invalid_identity(uid):
    fake_get, calls = source(uid=uid)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError, match="identidad"):
        client.account_uid()
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/uid"]


def test_account_uid_refuses_key_without_read_permission():
    fake_get, calls = source(permissions="SPOT_DEAL_READ")
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.account_uid()
    assert calls == ["/api/v3/apiKeyInfo"]


def test_order_query_is_signed_readonly_and_marks_observation_not_accounting():
    fake_get, calls = source()
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    observed = client.order_by_client_id("BTCUSDT", "a" * 32)
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/order"]
    assert observed == {
        "source": "MEXC_SPOT_PRIVATE_READONLY", "symbol": "BTCUSDT",
        "client_order_id": "a" * 32, "order_id": "order-1",
        "side": "BUY", "type": "LIMIT",
        "status": "PARTIALLY_FILLED", "price": "65000.00", "quantity": "0.01",
        "executed_quantity": "0.002", "cumulative_quote": "130",
        "accounting": "OBSERVED_NOT_IMPORTED",
    }


def test_order_query_requires_deal_read_and_valid_identifiers():
    fake_get, calls = source(permissions="SPOT_ACCOUNT_READ")
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError, match="SPOT_DEAL_READ"):
        client.order_by_client_id("BTCUSDT", "a" * 32)
    assert calls == ["/api/v3/apiKeyInfo"]
    with pytest.raises(MexcReadOnlyError):
        client.order_by_client_id("bad", "a" * 32)
    with pytest.raises(MexcReadOnlyError):
        client.order_by_client_id("BTCUSDT", "not-an-id")
    assert calls == ["/api/v3/apiKeyInfo"]


@pytest.mark.parametrize("bad", [
    {"clientOrderId": "b" * 32}, {"symbol": "ETHUSDT"},
    {"type": "MARKET"}, {"status": "EXPIRED"},
    {"executedQty": "0.02"}, {"executedQty": "NaN"},
    {"status": "NEW"}, {"status": "FILLED"},
    {"Qty": "0.001"}, {"cumulativeQuoteQty": "-1"},
])
def test_order_query_rejects_mismatched_or_incoherent_response(bad):
    base = {"symbol": "BTCUSDT", "clientOrderId": "a" * 32, "orderId": "order-1",
            "price": "65000.00", "Qty": "0.01", "executedQty": "0.002",
            "cumulativeQuoteQty": "130", "status": "PARTIALLY_FILLED",
            "type": "LIMIT", "side": "BUY"}
    fake_get, calls = source(order=base | bad)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.order_by_client_id("BTCUSDT", "a" * 32)
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/order"]


def test_order_fills_are_filtered_by_external_order_id():
    fake_get, calls = source()
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    result = client.trades_for_order("BTCUSDT", "order-1")
    assert result["coverage"] == "ORDER_ID_BELOW_PAGE_LIMIT"
    assert result["trades"][0]["order_id"] == "order-1"
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/myTrades"]


def test_order_fills_reject_other_order_and_full_page():
    wrong = [{"symbol": "BTCUSDT", "id": "fill-1", "orderId": "other",
              "price": "65000", "qty": "0.002", "quoteQty": "130", "commission": "0",
              "commissionAsset": "USDT", "time": 1_799_999_999_000, "isBuyer": True}]
    fake_get, _ = source(order_trades=wrong)
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError, match="ajenos"):
        client.trades_for_order("BTCUSDT", "order-1")
    with pytest.raises(MexcReadOnlyError):
        client.trades_for_order("BTCUSDT", "bad id")


def test_recent_trades_are_exact_observed_fills_not_portfolio_events():
    fake_get, calls = source()
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    result = client.trades("BTCUSDT")
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/myTrades"]
    assert result["accounting"] == "OBSERVED_NOT_IMPORTED"
    assert result["coverage"] == "RECENT_MONTH_SINGLE_PAGE_INCOMPLETE"
    assert result["trades"] == [{
        "id": "fill-1", "order_id": "order-1", "side": "BUY",
        "time_ms": 1_799_999_999_000, "commission_asset": "BNB",
        "price": "65000.12345678", "quantity": "0.000000000000000123",
        "quote_quantity": "0.00000000799590777778", "commission": "0.00000001",
    }]


def test_recent_window_splits_saturated_pages_without_dropping_fills():
    now_ms = 1_800_000_000_000
    start_ms = now_ms - 7 * 86_400_000
    rows = [{"symbol": "BTCUSDT", "id": f"fill-{index}", "orderId": f"order-{index}",
             "price": "100.00", "qty": "0.01", "quoteQty": "1.00", "commission": "0",
             "commissionAsset": "USDT", "time": start_ms + (index + 1) * 600_000_000 // 1002,
             "isBuyer": True} for index in range(1001)]
    windows = []

    def fake_get(url, headers):
        parsed = urlparse(url)
        assert parsed.path in ("/api/v3/apiKeyInfo", "/api/v3/myTrades")
        unsigned, signature = parsed.query.rsplit("&signature=", 1)
        assert signature == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        assert headers == {"X-MEXC-APIKEY": KEY}
        params = parse_qs(unsigned)
        if parsed.path.endswith("apiKeyInfo"):
            return {"status": "VALID", "permissions": "SPOT_ACCOUNT_READ", "apikey": KEY}
        first, last = int(params["startTime"][0]), int(params["endTime"][0])
        assert params["limit"] == ["1000"] and first <= last
        windows.append((first, last))
        return [row for row in rows if first <= row["time"] <= last][:1000]

    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get, clock_ms=lambda: now_ms)
    result = client.trades_window("BTCUSDT", days=7)
    assert result["start_ms"] == start_ms and result["end_ms"] == now_ms
    assert result["pages"] == 3 and len(windows) == 3
    assert len(result["trades"]) == 1001
    assert len({row["id"] for row in result["trades"]}) == 1001
    assert result["coverage"] == "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT"


def test_window_rejects_dense_unverifiable_response_and_wrong_time():
    now_ms = 1_800_000_000_000

    def fake_get(url, _headers):
        if urlparse(url).path.endswith("apiKeyInfo"):
            return {"status": "VALID", "permissions": "SPOT_ACCOUNT_READ", "apikey": KEY}
        params = parse_qs(urlparse(url).query)
        first = int(params["startTime"][0])
        return [{"symbol": "BTCUSDT", "id": f"fill-{index}", "orderId": f"order-{index}",
                 "price": "1", "qty": "1", "quoteQty": "1", "commission": "0",
                 "commissionAsset": "USDT", "time": first, "isBuyer": True}
                for index in range(1000)]

    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get, clock_ms=lambda: now_ms)
    with pytest.raises(MexcReadOnlyError, match="denso"):
        client.trades_window("BTCUSDT", days=1)
    with pytest.raises(MexcReadOnlyError, match="Periodo"):
        client.trades_window("BTCUSDT", days=31)


@pytest.mark.parametrize("changes", [
    {"symbol": "ETHUSDT"}, {"id": "fill/unsafe"}, {"qty": "NaN"},
    {"price": "0"}, {"quoteQty": "-1"}, {"commission": "Infinity"},
    {"qty": "1e-100000"},
    {"commissionAsset": "bad asset"}, {"time": True}, {"isBuyer": "true"},
])
def test_rejects_malformed_trade_rows(changes):
    row = {"symbol": "BTCUSDT", "id": "fill-1", "orderId": "order-1",
           "price": "1", "qty": "2", "quoteQty": "2", "commission": "0",
           "commissionAsset": "USDT", "time": 1_799_999_999_000, "isBuyer": False}
    row.update(changes)
    fake_get, _ = source(trades=[row])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.trades("BTCUSDT")


def test_rejects_duplicate_fill_and_invalid_market_without_network():
    row = {"symbol": "BTCUSDT", "id": "fill-1", "orderId": "order-1",
           "price": "1", "qty": "2", "quoteQty": "2", "commission": "0",
           "commissionAsset": "USDT", "time": 1_799_999_999_000, "isBuyer": False}
    fake_get, calls = source(trades=[row, row])
    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.trades("BTCUSDT")
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/myTrades"]
    with pytest.raises(MexcReadOnlyError):
        client.trades("BTC/USDT")
    assert len(calls) == 2


@pytest.mark.parametrize("validity", [-1000, -1, 0, "30"])
def test_rejects_unverifiable_or_expired_key_validity(validity):
    def fake_get(url, headers):
        return {"status": "VALID", "permissions": "SPOT_ACCOUNT_READ",
                "apikey": KEY, "remainingValidity": validity}

    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcReadOnlyError):
        client.verify_permissions()


def test_accepts_permanent_readonly_key_validity():
    def fake_get(url, headers):
        return {"status": "VALID", "permissions": "SPOT_ACCOUNT_READ",
                "apikey": KEY, "remainingValidity": -999}

    client = MexcReadOnlyClient(KEY, SECRET, http_get=fake_get,
                                clock_ms=lambda: 1_800_000_000_000)
    assert client.verify_permissions() == ("SPOT_ACCOUNT_READ",)
