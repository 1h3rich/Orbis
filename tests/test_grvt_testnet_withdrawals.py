"""Retiros testnet: historia paginada, identidad y minimización de datos."""

from decimal import Decimal

import pytest

from app.live_execution.grvt_testnet_withdrawals import (
    GrvtTestnetWithdrawalError, audit_testnet_withdrawals,
)


SUB = "123"
START = 1_780_000_000_000_000_000
NOW = START + 10_000


def row(tx_id: str, *, owner: str = SUB, amount: str = "1.25",
        l1_hash: str = "") -> dict:
    return {"tx_id": tx_id, "from_account_id": owner,
            "event_time": str(START + 100), "currency": "USDT",
            "num_tokens": amount, "l_1_hash": l1_hash,
            "to_eth_address": "0xSensitiveDestination",
            "signature": {"signer": "0xSensitiveSigner"},
            "l_2_hash": "0xSensitiveHash"}


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def read_withdrawal_history(self, sub, start, end, cursor):
        self.calls.append((sub, start, end, cursor))
        return self.pages[len(self.calls) - 1]


def test_drains_pages_and_keeps_only_sanitized_target_rows():
    client = FakeClient([
        {"result": [row("pending"), row("other", owner="999")], "next": "page2"},
        {"result": [row("final", amount="2.50", l1_hash="0xhash")], "next": ""},
    ])
    audit = audit_testnet_withdrawals(client, SUB, start_time_ns=START,
                                     now_ns=NOW)
    assert audit.pages_drained == 2 and audit.unrelated_row_count == 1
    assert [(r.tx_id, r.status, r.amount) for r in audit.withdrawals] == [
        ("final", "FINALIZED", Decimal("2.50")),
        ("pending", "PENDING", Decimal("1.25")),
    ]
    assert audit.latest_withdrawal_time_ns == START + 100
    assert audit.complete_for_execution is False
    assert client.calls == [(SUB, str(START - 1), str(NOW), ""),
                            (SUB, str(START - 1), str(NOW), "page2")]
    assert "Sensitive" not in repr(audit)


@pytest.mark.parametrize("bad", [
    row("a", amount="0"), row("a", amount="nan"),
    row("a", owner="0xaddress"),
    {**row("a"), "event_time": str(START - 1)},
    {**row("a"), "currency": "../USDT"},
])
def test_rejects_malformed_relevant_rows(bad):
    client = FakeClient([{"result": [bad], "next": ""}])
    with pytest.raises(GrvtTestnetWithdrawalError):
        audit_testnet_withdrawals(client, SUB, start_time_ns=START, now_ns=NOW)


def test_rejects_duplicates_and_repeating_cursor():
    with pytest.raises(GrvtTestnetWithdrawalError, match="duplicado"):
        audit_testnet_withdrawals(FakeClient([
            {"result": [row("a"), row("a")], "next": ""}]), SUB,
            start_time_ns=START, now_ns=NOW)
    with pytest.raises(GrvtTestnetWithdrawalError, match="Cursor"):
        audit_testnet_withdrawals(FakeClient([
            {"result": [], "next": "same"},
            {"result": [], "next": "same"}]), SUB,
            start_time_ns=START, now_ns=NOW)
