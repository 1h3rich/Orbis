"""Depósitos de main account: no se confunden con saldo de la subcuenta."""

from decimal import Decimal

import pytest

from app.live_execution.grvt_testnet_deposits import (
    GrvtTestnetDepositError, audit_testnet_deposits,
)


SUB = "123"
MAIN = "0x" + "a" * 40
START = 1_780_000_000_000_000_000
NOW = START + 10_000


def row(l1_hash: str, *, recipient: str = MAIN, amount: str = "1.25",
        confirmed: str = "") -> dict:
    return {"l_1_hash": l1_hash, "l_2_hash": "0xSensitiveL2",
            "to_account_id": recipient, "currency": "USDT",
            "num_tokens": amount, "initiated_time": str(START + 100),
            "confirmed_time": confirmed,
            "from_address": "0xSensitiveSource"}


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def read_deposit_history(self, sub, start, end, cursor):
        self.calls.append((sub, start, end, cursor))
        return self.pages[len(self.calls) - 1]

    def matches_main_account_id(self, candidate):
        return candidate == MAIN


def test_pages_status_and_privacy_without_subaccount_attribution():
    client = FakeClient([
        {"result": [row("0x12345678")], "next": "page2"},
        {"result": [row("0xabcdef12", amount="2.5",
                        confirmed=str(START + 200))], "next": ""},
    ])
    audit = audit_testnet_deposits(client, SUB, start_time_ns=START, now_ns=NOW)
    assert audit.pages_drained == 2
    assert sorted((r.status, r.amount) for r in audit.deposits) == sorted([
        ("PENDING", Decimal("1.25")), ("CONFIRMED", Decimal("2.5"))]
    )
    assert audit.latest_deposit_time_ns == START + 200
    assert not audit.attributed_to_sub_account
    assert not audit.complete_for_execution
    assert client.calls == [(SUB, str(START - 1), str(NOW), ""),
                            (SUB, str(START - 1), str(NOW), "page2")]
    assert "12345678" not in repr(audit)
    assert "Sensitive" not in repr(audit)
    assert MAIN not in repr(audit)


@pytest.mark.parametrize("bad", [
    row("0x12345678", recipient="0x" + "b" * 40),
    row("0x12345678", amount="0"),
    row("0x12345678", amount="NaN"),
    row("bad-hash"),
    row("0x12345678", confirmed=str(START + 50)),
    {**row("0x12345678"), "initiated_time": str(START - 1)},
])
def test_rejects_wrong_main_account_and_malformed_rows(bad):
    with pytest.raises(GrvtTestnetDepositError):
        audit_testnet_deposits(FakeClient([{"result": [bad], "next": ""}]),
                               SUB, start_time_ns=START, now_ns=NOW)


def test_rejects_duplicate_and_repeated_cursor():
    with pytest.raises(GrvtTestnetDepositError, match="duplicado"):
        audit_testnet_deposits(FakeClient([{
            "result": [row("0x12345678"), row("0x12345678")], "next": ""}]),
            SUB, start_time_ns=START, now_ns=NOW)
    with pytest.raises(GrvtTestnetDepositError, match="Cursor"):
        audit_testnet_deposits(FakeClient([
            {"result": [], "next": "same"},
            {"result": [], "next": "same"}]),
            SUB, start_time_ns=START, now_ns=NOW)
