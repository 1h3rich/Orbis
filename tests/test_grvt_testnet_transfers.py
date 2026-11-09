from decimal import Decimal

import pytest

from app.live_execution.grvt_testnet_transfers import (
    GrvtTestnetTransferError, audit_testnet_transfers,
)


NOW = 1_780_000_000_000_000_000
START = NOW - 10_000_000_000
SUB = "123456"


def transfer(*, tx_id="tx-1", from_sub="0", to_sub=SUB,
             amount="5", event_time=NOW - 1):
    return {"tx_id": tx_id, "from_sub_account_id": from_sub,
            "to_sub_account_id": to_sub, "currency": "USDT",
            "num_tokens": amount, "event_time": str(event_time),
            "from_account_id": "0xFullPrivateSource",
            "to_account_id": "0xFullPrivateDestination",
            "signature": {"signer": "0xFullPrivateSigner"}}


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def read_transfer_history(self, sub, start, end, cursor):
        self.calls.append((sub, start, end, cursor))
        return self.pages[cursor]


def test_transfers_drain_pages_and_count_only_target_subaccount():
    client = FakeClient({
        "": {"result": [transfer(), transfer(tx_id="other", to_sub="999")],
             "next": "more"},
        "more": {"result": [transfer(tx_id="tx-2", from_sub=SUB, to_sub="0",
                                      amount="2"),
                             transfer(tx_id="tx-3", from_sub=SUB, to_sub=SUB,
                                      amount="1")], "next": ""},
    })
    audit = audit_testnet_transfers(client, SUB,
                                    start_time_ns=START, now_ns=NOW)
    assert audit.net_by_currency == (("USDT", Decimal("3")),)
    assert [row.direction for row in audit.transfers] == ["IN", "OUT", "SELF"]
    assert audit.unrelated_row_count == 1
    assert audit.pages_drained == 2
    assert audit.complete_for_execution is False
    assert "FullPrivate" not in str(audit)
    assert client.calls == [(SUB, str(START - 1), str(NOW), ""),
                            (SUB, str(START - 1), str(NOW), "more")]


@pytest.mark.parametrize("row", [
    transfer(amount="0"),
    transfer(amount="NaN"),
    transfer(event_time=START - 1),
    transfer(tx_id="bad tx"),
    transfer(from_sub="0xprivate"),
    {**transfer(), "currency": "0xaddress"},
])
def test_transfer_rejects_invalid_target_row(row):
    client = FakeClient({"": {"result": [row], "next": ""}})
    with pytest.raises(GrvtTestnetTransferError):
        audit_testnet_transfers(client, SUB, start_time_ns=START, now_ns=NOW)


def test_transfer_rejects_duplicate_and_cursor_loop():
    client = FakeClient({"": {"result": [transfer(), transfer()], "next": ""}})
    with pytest.raises(GrvtTestnetTransferError, match="duplicada"):
        audit_testnet_transfers(client, SUB, start_time_ns=START, now_ns=NOW)
    client = FakeClient({"": {"result": [], "next": "again"},
                         "again": {"result": [], "next": "again"}})
    with pytest.raises(GrvtTestnetTransferError, match="Cursor"):
        audit_testnet_transfers(client, SUB, start_time_ns=START, now_ns=NOW)


def test_transfer_window_is_checked_before_network():
    client = FakeClient({})
    with pytest.raises(GrvtTestnetTransferError, match="Ventana"):
        audit_testnet_transfers(client, SUB, start_time_ns=NOW + 1, now_ns=NOW)
    assert client.calls == []
