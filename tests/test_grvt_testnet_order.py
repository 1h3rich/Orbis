"""El transporte GRVT testnet no toca la red ni fondos en estas pruebas."""

from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from app.live_execution.grvt_testnet_order import (
    AUTH_URL, ORDER_URL, LOOKUP_URL, FILL_HISTORY_URL, FUNDING_HISTORY_URL,
    TRANSFER_HISTORY_URL, WITHDRAWAL_HISTORY_URL, DEPOSIT_HISTORY_URL,
    INTEREST_HISTORY_URL, CANCEL_URL,
    GrvtTestnetLimits, GrvtTestnetOrderClient,
    GrvtTestnetOrderError,
)


NOW = 1_780_000_000_000_000_000
SUB = "123456"
ACCOUNT = "0x" + "a" * 40
CLIENT_ID = str(2**63 + 42)


def signed_order() -> dict:
    return {
        "sub_account_id": SUB,
        "is_market": False,
        "time_in_force": "IMMEDIATE_OR_CANCEL",
        "post_only": False,
        "reduce_only": False,
        "legs": [{"instrument": "BTC_USDT_Perp", "size": "0.01",
                  "limit_price": "50000", "is_buying_asset": True}],
        "signature": {"signer": ACCOUNT, "r": "0x" + "1" * 64,
                      "s": "0x" + "2" * 64, "v": 27,
                      "expiration": str(NOW + 60_000_000_000), "nonce": 1,
                      "chain_id": "326"},
        "metadata": {"client_order_id": CLIENT_ID},
        "builder": ACCOUNT,
        "builder_fee": "0",
    }


def response(url: str, payload: dict | None = None, *, status: int = 200) -> httpx.Response:
    request = httpx.Request("POST", url)
    headers = ({"X-Grvt-Account-Id": ACCOUNT,
                "Set-Cookie": "gravity=cookie-testnet-123; Path=/"}
               if url == AUTH_URL else {})
    return httpx.Response(status, json=payload, request=request,
                          headers=headers)


def limits() -> GrvtTestnetLimits:
    return GrvtTestnetLimits(SUB, "BTC_USDT_Perp", Decimal("1000"))


def client(calls: list, *, answer: object = None) -> GrvtTestnetOrderClient:
    def post(url: str, body: dict, headers: dict):
        calls.append((url, body, headers))
        if url == AUTH_URL:
            return response(url, {"status": "success", "sub_account_id": SUB})
        if isinstance(answer, Exception):
            raise answer
        return response(url, answer if answer is not None else {
            "result": {**signed_order(), "order_id": "0x123abc"}})
    return GrvtTestnetOrderClient("testnet-key-123", http_post=post, clock_ns=lambda: NOW)


def test_testnet_only_one_shot_and_ack_does_not_claim_fill():
    calls = []
    sender = client(calls)
    ack = sender.submit_signed_limit(signed_order(), limits())
    assert ack.accepted_by_testnet and ack.execution_unverified
    assert ack.client_order_id == CLIENT_ID and ack.order_id == "0x123abc"
    assert [call[0] for call in calls] == [AUTH_URL, ORDER_URL]
    assert all("testnet.grvt.io" in call[0] for call in calls)
    assert calls[1][1]["order"]["signature"]["chain_id"] == "326"
    assert "testnet-key-123" not in repr(sender)
    with pytest.raises(GrvtTestnetOrderError, match="consumida"):
        sender.submit_signed_limit(signed_order(), limits())
    assert len(calls) == 2


def test_funding_reader_is_testnet_bound_and_does_not_send_an_order():
    calls = []
    sender = client(calls, answer={"result": [], "next": ""})
    page = sender.read_funding_history(SUB, str(NOW - 10), str(NOW), "")
    assert page == {"result": [], "next": ""}
    assert [call[0] for call in calls] == [AUTH_URL, FUNDING_HISTORY_URL]
    assert calls[1][1] == {"sub_account_id": SUB,
                           "start_time": str(NOW - 10),
                           "end_time": str(NOW), "limit": 500, "cursor": ""}
    with pytest.raises(GrvtTestnetOrderError, match="fuera de alcance"):
        sender.read_funding_history("999", str(NOW - 10), str(NOW))
    assert len(calls) == 2


def test_fill_history_pins_end_time_without_submitting_orders():
    calls = []
    sender = client(calls,answer={"result":[],"next":""})
    assert sender.read_fill_history(SUB,str(NOW-10),"page",end_time_ns=str(NOW)) == {"result":[],"next":""}
    assert [call[0] for call in calls] == [AUTH_URL,FILL_HISTORY_URL]
    assert calls[-1][1] == {"sub_account_id":SUB,"start_time":str(NOW-10),
                           "end_time":str(NOW),"limit":500,"cursor":"page"}
    for invalid in (True,"",str(NOW-11),-1):
        with pytest.raises(GrvtTestnetOrderError):
            sender.read_fill_history(SUB,str(NOW-10),end_time_ns=invalid)
    assert len(calls) == 2


def test_transfer_reader_is_testnet_bound_and_requests_all_currencies():
    calls = []
    sender = client(calls, answer={"result": [], "next": ""})
    page = sender.read_transfer_history(SUB, str(NOW - 10), str(NOW), "")
    assert page == {"result": [], "next": ""}
    assert [call[0] for call in calls] == [AUTH_URL, TRANSFER_HISTORY_URL]
    assert calls[1][1] == {"currency": [], "start_time": str(NOW - 10),
                           "end_time": str(NOW), "limit": 500, "cursor": ""}
    with pytest.raises(GrvtTestnetOrderError, match="fuera de alcance"):
        sender.read_transfer_history("999", str(NOW - 10), str(NOW))
    assert len(calls) == 2


def test_withdrawal_reader_is_testnet_bound_and_requests_all_currencies():
    calls = []
    sender = client(calls, answer={"result": [], "next": ""})
    assert sender.read_withdrawal_history(SUB, str(NOW - 10), str(NOW)) == {
        "result": [], "next": ""}
    assert [call[0] for call in calls] == [AUTH_URL, WITHDRAWAL_HISTORY_URL]
    assert calls[1][1] == {"currency": [], "start_time": str(NOW - 10),
                           "end_time": str(NOW), "limit": 500, "cursor": ""}
    with pytest.raises(GrvtTestnetOrderError, match="fuera de alcance"):
        sender.read_withdrawal_history("999", str(NOW - 10), str(NOW))
    assert len(calls) == 2


def test_deposit_reader_is_testnet_bound_and_checks_main_account_in_memory():
    calls = []
    sender = client(calls, answer={"result": [], "next": ""})
    assert sender.read_deposit_history(SUB, str(NOW - 10), str(NOW)) == {
        "result": [], "next": ""}
    assert [call[0] for call in calls] == [AUTH_URL, DEPOSIT_HISTORY_URL]
    assert calls[1][1] == {"currency": [], "start_time": str(NOW - 10),
                           "end_time": str(NOW), "limit": 500, "cursor": ""}
    assert sender.matches_main_account_id(ACCOUNT)
    assert not sender.matches_main_account_id("0x" + "b" * 40)
    with pytest.raises(GrvtTestnetOrderError, match="fuera de alcance"):
        sender.read_deposit_history("999", str(NOW - 10), str(NOW))
    assert len(calls) == 2


def test_interest_reader_is_testnet_bound_and_unfiltered_by_currency():
    calls = []
    sender = client(calls, answer={"result": [], "next": ""})
    assert sender.read_interest_history(SUB, str(NOW - 10), str(NOW)) == {
        "result": [], "next": ""}
    assert [call[0] for call in calls] == [AUTH_URL, INTEREST_HISTORY_URL]
    assert calls[1][1] == {"sub_account_id": SUB,
                           "start_time": str(NOW - 10),
                           "end_time": str(NOW), "limit": 500, "cursor": ""}
    with pytest.raises(GrvtTestnetOrderError, match="fuera de alcance"):
        sender.read_interest_history("999", str(NOW - 10), str(NOW))
    assert len(calls) == 2


@pytest.mark.parametrize("change", [
    lambda o: o["signature"].update(chain_id="325"),
    lambda o: o["signature"].update(chain_id="0"),
    lambda o: o["signature"].update(expiration=str(NOW + 301_000_000_000)),
    lambda o: o["signature"].update(v=1),
    lambda o: o["metadata"].update(client_order_id="123"),
    lambda o: o.update(is_market=True),
    lambda o: o.update(time_in_force="GOOD_TILL_TIME"),
    lambda o: o.update(order_id="0x123"),
    lambda o: o["legs"].append(deepcopy(o["legs"][0])),
    lambda o: o["legs"][0].update(size="0.1"),
    lambda o: o.update(builder_fee="0.001"),
])
def test_invalid_order_never_reaches_exchange(change):
    calls = []
    sender = client(calls)
    order = signed_order()
    change(order)
    with pytest.raises(GrvtTestnetOrderError):
        sender.submit_signed_limit(order, limits())
    assert [call[0] for call in calls] == [AUTH_URL]


def test_wrong_bound_subaccount_never_sends():
    calls = []
    sender = client(calls)
    other = GrvtTestnetLimits("999", "BTC_USDT_Perp", Decimal("1000"))
    order = signed_order()
    order["sub_account_id"] = "999"
    with pytest.raises(GrvtTestnetOrderError, match="vinculada"):
        sender.submit_signed_limit(order, other)
    assert len(calls) == 1


@pytest.mark.parametrize("answer", [
    httpx.ReadTimeout("secret transport diagnostic"),
    {"result": {"sub_account_id": SUB, "order_id": "0x123abc",
                "metadata": {"client_order_id": "999"}}},
    {"result": {**signed_order(), "order_id": "0x123abc",
                "legs": [{**signed_order()["legs"][0], "is_buying_asset": False}]}},
    {"error": "private signer 0x123"},
])
def test_unknown_outcome_is_sanitized_and_not_retried(answer):
    calls = []
    sender = client(calls, answer=answer)
    with pytest.raises(GrvtTestnetOrderError, match="Resultado.*desconocido") as raised:
        sender.submit_signed_limit(signed_order(), limits())
    assert "secret" not in str(raised.value) and "0x123" not in str(raised.value)
    with pytest.raises(GrvtTestnetOrderError, match="consumida"):
        sender.submit_signed_limit(signed_order(), limits())
    assert len(calls) == 2


def observed_order(*, status="FILLED", client_id=CLIENT_ID, sub_id=SUB,
                   traded="0.01", side=True) -> dict:
    return {
        **signed_order(), "sub_account_id": sub_id, "order_id": "0x123abc",
        "legs": [{**signed_order()["legs"][0], "is_buying_asset": side}],
        "metadata": {"client_order_id": client_id, "create_time": str(NOW - 10_000_000_000)},
        "state": {"status": status, "traded_size": [traded],
                  "update_time": str(NOW - 1_000_000_000)},
    }


def test_lookup_after_ambiguous_submission_does_not_resend():
    calls = []

    def post(url, body, headers):
        calls.append((url, body, headers))
        if url == AUTH_URL:
            return response(url, {"status": "success", "sub_account_id": SUB})
        if url == ORDER_URL:
            raise httpx.ReadTimeout("possibly accepted")
        assert url == LOOKUP_URL
        return response(url, {"result": observed_order()})

    sender = GrvtTestnetOrderClient("testnet-key-123", http_post=post, clock_ns=lambda: NOW)
    with pytest.raises(GrvtTestnetOrderError, match="desconocido"):
        sender.submit_signed_limit(signed_order(), limits())
    observed = sender.lookup_order(SUB, CLIENT_ID)
    assert observed.status == "FILLED" and observed.traded_size == "0.01"
    assert observed.side == "BUY" and observed.instrument == "BTC_USDT_Perp"
    assert observed.execution_unverified
    assert [call[0] for call in calls] == [AUTH_URL, ORDER_URL, LOOKUP_URL]
    assert calls[2][1] == {"sub_account_id": SUB, "client_order_id": CLIENT_ID}


@pytest.mark.parametrize("row", [
    None,
    observed_order(client_id="999"),
    observed_order(sub_id="999"),
    observed_order(traded="0.02"),
    observed_order(status="UNKNOWN"),
    observed_order(side="yes"),
])
def test_lookup_rejects_absent_cross_account_and_malformed_state(row):
    calls = []

    def post(url, body, headers):
        calls.append(url)
        if url == AUTH_URL:
            return response(url, {"status": "success", "sub_account_id": SUB})
        return response(url, {"result": row})

    sender = GrvtTestnetOrderClient("testnet-key-123", http_post=post, clock_ns=lambda: NOW)
    with pytest.raises(GrvtTestnetOrderError, match="no verificable"):
        sender.lookup_order(SUB, CLIENT_ID)
    assert calls == [AUTH_URL, LOOKUP_URL]


def test_lookup_rejects_other_subaccount_before_http():
    calls = []
    sender = client(calls)
    with pytest.raises(GrvtTestnetOrderError, match="Identidad"):
        sender.lookup_order("999", CLIENT_ID)
    assert len(calls) == 1


def test_cancel_by_client_id_is_one_shot_testnet_only_and_not_reconciled():
    calls = []

    def post(url, body, headers):
        calls.append((url, body))
        if url == AUTH_URL:
            return response(url, {"status": "success", "sub_account_id": SUB})
        if url == CANCEL_URL:
            return response(url, {"result": {"ack": True}})
        assert url == LOOKUP_URL
        return response(url, {"result": observed_order(status="CANCELLED", traded="0")})

    sender = GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                     clock_ns=lambda: NOW)
    observed = sender.cancel_by_client_id(SUB, CLIENT_ID)
    assert observed.status == "CANCELLED" and observed.execution_unverified
    assert calls[1][1] == {"sub_account_id": SUB,
                           "client_order_id": CLIENT_ID,
                           "time_to_live_ms": "5000"}
    assert all("testnet.grvt.io" in url for url, _ in calls)
    with pytest.raises(GrvtTestnetOrderError, match="consumida"):
        sender.cancel_by_client_id(SUB, CLIENT_ID)
    assert len(calls) == 3


def test_cancel_timeout_is_unknown_and_never_retried_on_same_client():
    calls = []

    def post(url, body, headers):
        calls.append(url)
        if url == AUTH_URL:
            return response(url, {"status": "success", "sub_account_id": SUB})
        raise httpx.ReadTimeout("secret external response")

    sender = GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                     clock_ns=lambda: NOW)
    with pytest.raises(GrvtTestnetOrderError, match="desconocido") as raised:
        sender.cancel_by_client_id(SUB, CLIENT_ID)
    assert "secret" not in str(raised.value)
    with pytest.raises(GrvtTestnetOrderError, match="consumida"):
        sender.cancel_by_client_id(SUB, CLIENT_ID)
    assert calls == [AUTH_URL, CANCEL_URL]


def test_cancel_rejects_cross_account_before_post():
    calls = []
    sender = client(calls)
    with pytest.raises(GrvtTestnetOrderError, match="Identidad"):
        sender.cancel_by_client_id("999", CLIENT_ID)
    assert len(calls) == 1
