from decimal import Decimal

import pytest

from app.live_execution.grvt_testnet_funding import (
    GrvtTestnetFundingError, audit_testnet_funding, linked_testnet_funding,
)


NOW = 1_780_000_000_000_000_000
START = NOW - 10_000_000_000
SUB = "123456"


def payment(*, tx_id="tx-1", event_time=NOW - 1,
            amount="0.25", sub=SUB, instrument="BTC_USDT_Perp"):
    return {"tx_id": tx_id, "event_time": str(event_time),
            "sub_account_id": sub, "instrument": instrument,
            "currency": "USDT", "amount": amount}


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def read_funding_history(self, sub, start, end, cursor):
        self.calls.append((sub, start, end, cursor))
        return self.pages[cursor]


def test_funding_drains_pages_and_preserves_paid_received_signs():
    client = FakeClient({
        "": {"result": [payment()], "next": "more"},
        "more": {"result": [payment(tx_id="tx-2", amount="-0.10")],
                 "next": ""},
    })
    result = audit_testnet_funding(client, SUB,
                                   start_time_ns=START, now_ns=NOW)
    assert result.paid_by_currency == (("USDT", Decimal("0.15")),)
    assert result.pages_drained == 2
    assert len(result.payments) == 2
    assert result.latest_payment_time_ns == NOW - 1
    assert result.complete_for_execution is False
    assert client.calls == [
        (SUB, str(START - 1), str(NOW), ""),
        (SUB, str(START - 1), str(NOW), "more"),
    ]


@pytest.mark.parametrize("row", [
    payment(sub="999"),
    payment(event_time=START - 1),
    payment(amount="NaN"),
    payment(tx_id="bad tx"),
    {**payment(), "currency": "0xprivate"},
])
def test_funding_rejects_bad_or_cross_scope_rows(row):
    client = FakeClient({"": {"result": [row], "next": ""}})
    with pytest.raises(GrvtTestnetFundingError):
        audit_testnet_funding(client, SUB, start_time_ns=START, now_ns=NOW)


def test_funding_rejects_duplicate_and_cursor_loop():
    client = FakeClient({"": {"result": [payment(), payment()], "next": ""}})
    with pytest.raises(GrvtTestnetFundingError, match="duplicado"):
        audit_testnet_funding(client, SUB, start_time_ns=START, now_ns=NOW)
    client = FakeClient({"": {"result": [], "next": "more"},
                         "more": {"result": [], "next": "more"}})
    with pytest.raises(GrvtTestnetFundingError, match="Cursor"):
        audit_testnet_funding(client, SUB, start_time_ns=START, now_ns=NOW)


def test_funding_rejects_invalid_window_before_network():
    client = FakeClient({})
    with pytest.raises(GrvtTestnetFundingError, match="Ventana"):
        audit_testnet_funding(client, SUB, start_time_ns=NOW + 1, now_ns=NOW)
    assert client.calls == []


def funding_payload():
    return {"payment_count":1,"paid_by_currency":[["USDT","0.250000000000000001"]],
        "rows":[{"tx_id":"own-fill","event_time_ns":NOW-1,"instrument":"BTC_USDT_Perp",
                 "currency":"USDT","amount_paid":"0.250000000000000001"}]}


def test_linked_funding_preserves_paid_received_signs_and_exact_precision():
    data = funding_payload()
    data["rows"].append({**data["rows"][0],"tx_id":"own-fill-2","amount_paid":"-0.10"})
    data["payment_count"] = 2
    data["paid_by_currency"] = [["USDT","0.150000000000000001"]]
    result = linked_testnet_funding(data,[{"trade_id":"own-fill","event_time_ns":NOW-2},
        {"trade_id":"own-fill-2","event_time_ns":NOW-2}],instrument="BTC_USDT_Perp",start_ns=START,end_ns=NOW)
    assert result["paid_usdt"] == "0.150000000000000001"
    assert result["rows"] == data["rows"] and result["payment_count"] == 2


@pytest.mark.parametrize("fault",["foreign_fill","instrument","currency","before_fill","after_cutoff",
    "boolean_time","duplicate","boolean_count","count","total","private_field","empty_total"])
def test_linked_funding_rejects_unattributed_or_inconsistent_data(fault):
    data = funding_payload(); row = data["rows"][0]
    if fault == "foreign_fill": row["tx_id"] = "external"
    if fault == "instrument": row["instrument"] = "ETH_USDT_Perp"
    if fault == "currency": row["currency"] = "USDC"
    if fault == "before_fill": row["event_time_ns"] = NOW-3
    if fault == "after_cutoff": row["event_time_ns"] = NOW+1
    if fault == "boolean_time": row["event_time_ns"] = True
    if fault == "duplicate": data["rows"].append(dict(row)); data["payment_count"] = 2
    if fault == "boolean_count": data["payment_count"] = True
    if fault == "count": data["payment_count"] = 2
    if fault == "total": data["paid_by_currency"] = [["USDT","0"]]
    if fault == "private_field": row["signature"] = "must-not-be-accepted"
    if fault == "empty_total": data["rows"] = []; data["payment_count"] = 0
    with pytest.raises(GrvtTestnetFundingError):
        linked_testnet_funding(data,[{"trade_id":"own-fill","event_time_ns":NOW-2}],
            instrument="BTC_USDT_Perp",start_ns=START,end_ns=NOW)
