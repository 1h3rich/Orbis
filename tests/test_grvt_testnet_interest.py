"""Interés MAM testnet observado sin inferir signo contable."""

from decimal import Decimal

import pytest

from app.live_execution.grvt_testnet_interest import (
    GrvtTestnetInterestError, audit_testnet_interest,
)


SUB = "123"
START = 1_780_000_000_000_000_000
NOW = START + 10_000


def row(*, currency="USDT", amount="1.25", event=START + 100,
        charge=START + 50, sub=SUB, borrowed="100") -> dict:
    return {"event_time": str(event), "charge_time": str(charge),
            "sub_account_id": sub, "currency": currency,
            "amount": amount, "borrowed_amount": borrowed}


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def read_interest_history(self, sub, start, end, cursor):
        self.calls.append((sub, start, end, cursor))
        return self.pages[len(self.calls) - 1]


def test_drains_pages_and_preserves_reported_sign():
    client = FakeClient([
        {"result": [row()], "next": "page2"},
        {"result": [row(currency="USDC", amount="-0.25",
                        event=START + 200, charge=START + 150)], "next": ""},
    ])
    audit = audit_testnet_interest(client, SUB, start_time_ns=START, now_ns=NOW)
    assert audit.pages_drained == 2
    assert audit.reported_by_currency == (("USDC", Decimal("-0.25")),
                                          ("USDT", Decimal("1.25")))
    assert audit.payments[0].borrowed_amount == Decimal("100")
    assert audit.latest_payment_time_ns == START + 200
    assert not audit.complete_for_execution
    assert client.calls == [(SUB, str(START - 1), str(NOW), ""),
                            (SUB, str(START - 1), str(NOW), "page2")]


@pytest.mark.parametrize("bad", [
    row(sub="999"), row(amount="nan"), row(borrowed="-1"),
    row(charge=START + 3_000_000_000), row(event=START - 1),
    row(currency="USDT/../../"),
])
def test_rejects_cross_account_and_malformed_rows(bad):
    with pytest.raises(GrvtTestnetInterestError):
        audit_testnet_interest(FakeClient([{"result": [bad], "next": ""}]),
                               SUB, start_time_ns=START, now_ns=NOW)


def test_rejects_duplicate_or_repeated_cursor():
    with pytest.raises(GrvtTestnetInterestError, match="duplicado"):
        audit_testnet_interest(FakeClient([{"result": [row(), row()],
                                           "next": ""}]), SUB,
                               start_time_ns=START, now_ns=NOW)
    with pytest.raises(GrvtTestnetInterestError, match="Cursor"):
        audit_testnet_interest(FakeClient([
            {"result": [], "next": "same"},
            {"result": [], "next": "same"}]), SUB,
            start_time_ns=START, now_ns=NOW)
