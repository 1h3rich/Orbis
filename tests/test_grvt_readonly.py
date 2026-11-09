"""GRVT privado: lectura acotada, cifras exactas y credenciales efímeras."""

from decimal import Decimal
import httpx
import pytest

from app.integrations.grvt_readonly import (AUTH_URL, TRADES_ROOT,
                                            GrvtReadOnlyClient, GrvtReadOnlyError)


KEY = "fixture-view-api-key"
ADDRESS = "0x" + "a" * 40
COOKIE = "fixture-gravity-cookie"


def transport(*, aggregate=None, account=None, sub_ids=None, bound_sub="123", open_orders=None,
              single_order=None, fill_pages=None):
    calls = []

    def post(url, payload, headers):
        calls.append((url, payload, headers))
        if url == AUTH_URL:
            assert payload == {"api_key": KEY}
            assert headers == {"Cookie": "rm=true;", "Content-Type": "application/json"}
            body = {"status": "success", "funding_account_address": ADDRESS}
            if bound_sub is not None:
                body["sub_account_id"] = bound_sub
            return httpx.Response(200, json=body, headers={
                "set-cookie": "gravity=" + COOKIE + "; Path=/; HttpOnly",
                "x-grvt-account-id": ADDRESS}, request=httpx.Request("POST", url))
        assert url.startswith(TRADES_ROOT + "/")
        assert headers == {"Cookie": "gravity=" + COOKIE,
                           "X-Grvt-Account-Id": ADDRESS,
                           "Content-Type": "application/json"}
        if url.endswith("aggregated_account_summary"):
            assert payload == {}
            return httpx.Response(200, json={"result": aggregate or {
                "main_account_id": ADDRESS, "total_equity": "110.000000000000000001",
                "total_sub_account_equity": "100.000000000000000001",
                "spot_balances": [{"currency": "USDT", "balance": "10.00"}]}})
        if url.endswith("get_sub_accounts"):
            assert payload == {}
            return httpx.Response(200, json={"sub_account_ids": sub_ids or ["123"]})
        if url.endswith("open_orders"):
            assert payload == {"sub_account_id": "123"}
            return httpx.Response(200, json={"result": [] if open_orders is None else open_orders})
        if url.endswith("/order"):
            assert payload == {"sub_account_id": "123", "client_order_id": "9223372036854775808"}
            return httpx.Response(200, json={"result": single_order})
        if url.endswith("fill_history"):
            assert payload["sub_account_id"] == "123"
            assert payload["start_time"] == "1799999999999999999"
            assert payload["limit"] == 500
            return httpx.Response(200, json=(fill_pages or {}).get(payload["cursor"],
                                         {"result": [], "next": ""}))
        assert url.endswith("account_summary") and payload == {"sub_account_id": "123"}
        return httpx.Response(200, json={"result": account or {
            "sub_account_id": "123", "settle_currency": "USDT",
            "event_time": "1800000000000000000", "total_equity": "100.000000000000000001",
            "maintenance_margin": "1.25", "unrealized_pnl": "-0.5",
            "spot_balances": [{"currency": "USDT", "balance": "100"}],
            "positions": [{"sub_account_id": "123", "instrument": "BTC_USDT_Perp",
                           "size": "-0.123456789012345678", "entry_price": "65000",
                           "mark_price": "66000", "unrealized_pnl": "-123.456789012345678"}]}})

    return post, calls


def test_grvt_snapshot_uses_only_documented_read_paths_and_keeps_decimals():
    post, calls = transport()
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    assert KEY not in repr(client) and COOKIE not in repr(client)
    result = client.snapshot()
    assert [url for url, _, _ in calls] == [AUTH_URL,
        TRADES_ROOT + "/aggregated_account_summary", TRADES_ROOT + "/account_summary"]
    assert result["source"] == "GRVT_MAINNET_PRIVATE_READONLY"
    assert result["accounting"] == "OBSERVED_NOT_IMPORTED"
    assert result["permission_verified"] is False
    assert Decimal(result["total_equity_usd"]) == Decimal("110.000000000000000001")
    assert result["spot_balances_aggregated"] == [{"asset": "USDT", "balance": "10.00"}]
    position = result["trading_accounts"][0]["positions"][0]
    assert position["side"] == "SHORT" and position["size"] == "-0.123456789012345678"
    assert position["unrealized_pnl"] == "-123.456789012345678"
    assert result["trading_accounts"][0]["fresh"] is True
    assert KEY not in str(result) and COOKIE not in str(result) and ADDRESS not in str(result)
    assert not hasattr(client, "_api_key")
    with pytest.raises(GrvtReadOnlyError, match="no permitida"):
        client._query("create_order")


def test_main_account_key_lists_accessible_subaccounts():
    post, calls = transport(bound_sub=None)
    result = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000).snapshot()
    assert result["trading_accounts"][0]["sub_account_id"] == "123"
    assert [url.rsplit("/", 1)[-1] for url, _, _ in calls] == [
        "login", "aggregated_account_summary", "get_sub_accounts", "account_summary"]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", 1.5])
def test_bad_balance_fails_closed(bad):
    post, _ = transport(aggregate={"main_account_id": ADDRESS,
        "total_equity": "100", "total_sub_account_equity": "0",
        "spot_balances": [{"currency": "BTC", "balance": bad}]})
    with pytest.raises(GrvtReadOnlyError):
        GrvtReadOnlyClient(KEY, http_post=post,
                           clock_ns=lambda: 1_800_000_000_000_000_000).snapshot()


def test_cross_account_or_malformed_position_is_rejected():
    post, _ = transport(account={"sub_account_id": "999", "positions": [],
                                  "total_equity": "1", "maintenance_margin": "0",
                                  "unrealized_pnl": "0", "spot_balances": []})
    with pytest.raises(GrvtReadOnlyError, match="otra subcuenta"):
        GrvtReadOnlyClient(KEY, http_post=post,
                           clock_ns=lambda: 1_800_000_000_000_000_000).snapshot()


def test_stale_trading_snapshot_is_labeled_and_future_is_rejected():
    post, _ = transport()
    old = GrvtReadOnlyClient(KEY, http_post=post,
                             clock_ns=lambda: 1_800_000_120_000_000_000).snapshot()
    assert old["trading_accounts"][0]["fresh"] is False
    post2, _ = transport()
    with pytest.raises(GrvtReadOnlyError, match="hora futura"):
        GrvtReadOnlyClient(KEY, http_post=post2,
                           clock_ns=lambda: 1_799_999_990_000_000_000).snapshot()


def test_authentication_error_does_not_expose_key_or_response():
    def rejected(url, payload, headers):
        return httpx.Response(403, json={"message": KEY})
    with pytest.raises(GrvtReadOnlyError) as error:
        GrvtReadOnlyClient(KEY, http_post=rejected)
    assert KEY not in str(error.value)


def _order(**changes):
    order = {"order_id": "0x123", "sub_account_id": "123", "reduce_only": True,
             "metadata": {"client_order_id": "9223372036854775808"},
             "state": {"status": "OPEN", "traded_size": ["0.1"],
                       "update_time": "1800000000000000000"},
             "legs": [{"instrument": "BTC_USDT_Perp", "size": "0.3",
                       "is_buying_asset": False, "limit_price": "65000"}],
             "signature": {"signer": ADDRESS, "r": "sensitive-r", "s": "sensitive-s"}}
    order.update(changes)
    return order


def test_open_orders_are_observational_and_strip_signature():
    post, calls = transport(open_orders=[_order()])
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    result = client.open_orders()
    assert [url.rsplit("/", 1)[-1] for url, _, _ in calls] == ["login", "open_orders"]
    assert result["complete_for_execution"] is False
    assert result["in_flight_orders_excluded_possible"] is True
    assert result["orders"] == [{"sub_account_id": "123", "order_id": "0x123",
        "client_order_id": "9223372036854775808", "status": "OPEN",
        "reduce_only": True, "legs": [{"instrument": "BTC_USDT_Perp",
        "side": "SELL", "size": "0.3", "traded_size": "0.1", "limit_price": "65000"}],
        "update_time_ns": "1800000000000000000"}]
    assert ADDRESS not in str(result) and "sensitive-r" not in str(result)


@pytest.mark.parametrize("bad", [
    _order(sub_account_id="999"),
    _order(state={"status": "FILLED", "traded_size": ["0.1"],
                  "update_time": "1800000000000000000"}),
    _order(state={"status": "OPEN", "traded_size": ["0.4"],
                  "update_time": "1800000000000000000"}),
    _order(legs=[]),
])
def test_open_orders_fail_closed_on_bad_account_state_or_amount(bad):
    post, _ = transport(open_orders=[bad])
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    with pytest.raises(GrvtReadOnlyError):
        client.open_orders()


def test_open_orders_reject_duplicate_client_ids():
    post, _ = transport(open_orders=[_order(), _order(order_id="0x124")])
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    with pytest.raises(GrvtReadOnlyError, match="duplicado"):
        client.open_orders()


def test_single_order_rechecks_terminal_state_without_signature():
    filled = _order(state={"status": "FILLED", "traded_size": ["0.3"],
                            "update_time": "1800000000000000000"})
    post, calls = transport(single_order=filled)
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    result = client.order("123", "9223372036854775808")
    assert [url.rsplit("/", 1)[-1] for url, _, _ in calls] == ["login", "order"]
    assert result["order"]["status"] == "FILLED"
    assert result["order"]["legs"][0]["traded_size"] == "0.3"
    assert result["complete_for_execution"] is False
    assert ADDRESS not in str(result) and "sensitive-r" not in str(result)


def test_single_order_rejects_foreign_subaccount_and_mismatched_id():
    post, calls = transport(single_order=_order(metadata={"client_order_id": "99"}))
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    with pytest.raises(GrvtReadOnlyError, match="no accesible"):
        client.order("999", "9223372036854775808")
    assert len(calls) == 1
    with pytest.raises(GrvtReadOnlyError, match="otra orden"):
        client.order("123", "9223372036854775808")


def test_single_order_rejects_malformed_or_future_state():
    post, _ = transport(single_order=_order(state={"status": "OPEN", "traded_size": ["0.1"],
                                                   "update_time": "1800000003000000000"}))
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    with pytest.raises(GrvtReadOnlyError, match="hora"):
        client.order("123", "9223372036854775808")


def _fill(trade_id, size, **changes):
    row = {"trade_id": trade_id, "event_time": "1800000000000000000",
           "sub_account_id": "123", "order_id": "0x123",
           "client_order_id": "9223372036854775808",
           "instrument": "BTC_USDT_Perp", "is_buyer": False, "is_taker": True,
           "size": size, "price": "65000", "fee": "0.12",
           "builder_fee": "0", "fee_currency": "USDT", "realized_pnl": "1.5",
           "signer": ADDRESS}
    row.update(changes)
    return row


def test_order_fills_drain_pages_and_match_exact_sizes_without_signer():
    order = _order(metadata={"client_order_id": "9223372036854775808",
                             "create_time": "1800000000000000000"},
                   state={"status": "FILLED", "traded_size": ["0.3"],
                          "update_time": "1800000000000000000"})
    pages = {"": {"result": [_fill("trade-1", "0.1")], "next": "cursor-2"},
             "cursor-2": {"result": [_fill("trade-2", "0.2")], "next": ""}}
    post, calls = transport(single_order=order, fill_pages=pages)
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    result = client.order_fills("123", "9223372036854775808")
    assert [url.rsplit("/", 1)[-1] for url, _, _ in calls] == [
        "login", "order", "fill_history", "fill_history"]
    assert result["history_pages_drained"] is True
    assert result["fill_sizes_match_order_state"] is True
    assert result["complete_for_execution"] is False
    assert [fill["size"] for fill in result["fills"]] == ["0.1", "0.2"]
    assert ADDRESS not in str(result)


def test_order_fills_mismatch_is_explicit_and_repeated_cursor_fails():
    order = _order(metadata={"client_order_id": "9223372036854775808",
                             "create_time": "1800000000000000000"},
                   state={"status": "FILLED", "traded_size": ["0.3"],
                          "update_time": "1800000000000000000"})
    post, _ = transport(single_order=order,
                        fill_pages={"": {"result": [_fill("trade-1", "0.1")], "next": ""}})
    client = GrvtReadOnlyClient(KEY, http_post=post,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    assert client.order_fills("123", "9223372036854775808")["fill_sizes_match_order_state"] is False
    post2, _ = transport(single_order=order,
                         fill_pages={"": {"result": [], "next": "same"},
                                     "same": {"result": [], "next": "same"}})
    client2 = GrvtReadOnlyClient(KEY, http_post=post2,
                                 clock_ns=lambda: 1_800_000_000_000_000_000)
    with pytest.raises(GrvtReadOnlyError, match="Cursor"):
        client2.order_fills("123", "9223372036854775808")


def test_order_fills_reject_duplicate_trade_and_missing_creation():
    no_created, _ = transport(single_order=_order())
    client = GrvtReadOnlyClient(KEY, http_post=no_created,
                                clock_ns=lambda: 1_800_000_000_000_000_000)
    with pytest.raises(GrvtReadOnlyError, match="creación"):
        client.order_fills("123", "9223372036854775808")
    order = _order(metadata={"client_order_id": "9223372036854775808",
                             "create_time": "1800000000000000000"})
    post, _ = transport(single_order=order,
                        fill_pages={"": {"result": [_fill("same", "0.1"),
                                                        _fill("same", "0.1")], "next": ""}})
    client2 = GrvtReadOnlyClient(KEY, http_post=post,
                                 clock_ns=lambda: 1_800_000_000_000_000_000)
    with pytest.raises(GrvtReadOnlyError, match="duplicado"):
        client2.order_fills("123", "9223372036854775808")
