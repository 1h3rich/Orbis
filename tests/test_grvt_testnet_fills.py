from decimal import Decimal

import pytest

from app.live_execution.grvt_testnet_fills import (
    GrvtTestnetFillError, audit_testnet_fills,
)
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderObservation


NOW_NS = 1_780_000_000_000_000_000
SUB = "123456"
CID = str(2**63 + 123)


def order(*, traded="0.01", created=str(NOW_NS - 10_000_000_000)):
    return GrvtTestnetOrderObservation(
        SUB, CID, "0x123abc", "FILLED", "BTC_USDT_Perp", "BUY",
        "0.01", traded, "50000", str(NOW_NS), create_time_ns=created,
    )


def fill(*, trade_id="trade-1", size="0.01", order_id="0x123abc",
         client_id=CID, sub=SUB, fee="0.25", realized_pnl="0",
         builder_fee="0", fee_currency="USDT"):
    return {
        "sub_account_id": sub, "order_id": order_id,
        "client_order_id": client_id, "trade_id": trade_id,
        "instrument": "BTC_USDT_Perp", "is_buyer": True,
        "size": size, "price": "50000", "event_time": str(NOW_NS - 1),
        "is_taker": True, "is_liquidation": False,
        "fee": fee, "realized_pnl": realized_pnl,
        "builder_fee": builder_fee, "fee_currency": fee_currency,
    }


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def read_fill_history(self, sub, start, cursor):
        self.calls.append((sub, start, cursor))
        return self.pages[cursor]


def test_audit_drains_pages_and_matches_exact_fill_size():
    client = FakeClient({
        "": {"result": [fill(size="0.004")], "next": "next"},
        "next": {"result": [fill(trade_id="trade-2", size="0.006")], "next": ""},
    })
    result = audit_testnet_fills(client, order(), now_ns=NOW_NS)
    assert result.fill_count == 2
    assert result.filled_size == Decimal("0.010")
    assert result.sizes_match is True
    assert result.pages_drained == 2
    assert result.gross_notional == Decimal("500.000")
    assert result.realized_pnl == Decimal("0")
    assert result.builder_fee == Decimal("0")
    assert result.fees_by_currency == (("USDT", Decimal("0.50")),)
    assert result.complete_for_execution is False
    assert client.calls == [
        (SUB, str(NOW_NS - 10_000_000_001), ""),
        (SUB, str(NOW_NS - 10_000_000_001), "next"),
    ]


def test_partial_or_missing_history_never_matches():
    client = FakeClient({"": {"result": [], "next": ""}})
    result = audit_testnet_fills(client, order(), now_ns=NOW_NS)
    assert result.sizes_match is False
    assert result.fill_count == 0


@pytest.mark.parametrize("row", [
    fill(order_id="0xwrong"),
    fill(client_id="999"),
    fill(sub="999"),
    fill(size="0"),
    fill(size="NaN"),
    {**fill(), "is_buyer": False},
    {**fill(), "fee": "NaN"},
    {**fill(), "realized_pnl": "Infinity"},
    {**fill(), "fee_currency": "0xprivate"},
    {**fill(), "is_liquidation": True},
    {**fill(), "is_taker": "true"},
])
def test_bad_or_cross_scope_fill_fails_closed(row):
    client = FakeClient({"": {"result": [row], "next": ""}})
    with pytest.raises(GrvtTestnetFillError):
        audit_testnet_fills(client, order(), now_ns=NOW_NS)


def test_duplicate_and_cursor_loop_fail_closed():
    client = FakeClient({"": {"result": [fill(), fill()], "next": ""}})
    with pytest.raises(GrvtTestnetFillError, match="duplicado"):
        audit_testnet_fills(client, order(), now_ns=NOW_NS)
    client = FakeClient({"": {"result": [], "next": "again"},
                         "again": {"result": [], "next": "again"}})
    with pytest.raises(GrvtTestnetFillError, match="Cursor"):
        audit_testnet_fills(client, order(traded="0"), now_ns=NOW_NS)


def test_order_without_creation_time_cannot_be_audited():
    client = FakeClient({})
    with pytest.raises(GrvtTestnetFillError, match="Creación"):
        audit_testnet_fills(client, order(created=None), now_ns=NOW_NS)
    assert client.calls == []


def test_signed_rebate_and_realized_pnl_are_kept_without_settlement_claim():
    client = FakeClient({"": {"result": [fill(fee="-0.10", realized_pnl="-2.50",
                                                 builder_fee="0.01")], "next": ""}})
    result = audit_testnet_fills(client, order(), now_ns=NOW_NS)
    assert result.fees_by_currency == (("USDT", Decimal("-0.10")),)
    assert result.realized_pnl == Decimal("-2.50")
    assert result.builder_fee == Decimal("0.01")
    assert result.sizes_match is True
    assert result.complete_for_execution is False


def test_other_order_fill_between_preflight_and_order_is_archivable_evidence():
    created = NOW_NS - 10_000_000_000
    preflight = created - 2_000_000_000
    external = {**fill(trade_id="other-1", order_id="0xother",
                       client_id=str(2**63 + 124)),
                "event_time": str(created - 1)}
    client = FakeClient({"": {"result": [external, fill()], "next": ""}})
    result = audit_testnet_fills(client, order(), now_ns=NOW_NS,
                                 start_time_ns=preflight)
    assert result.sizes_match is True
    assert result.fill_count == 1
    assert len(result.other_fills) == 1
    assert result.other_fills[0].trade_id == "other-1"
    assert result.other_fills[0].order_id == "0xother"
    assert result.history_start_ns == preflight
    assert client.calls == [(SUB, str(preflight - 1), "")]
    assert result.complete_for_execution is False


def test_external_fill_duplicate_or_malformed_does_not_disappear_silently():
    external = fill(trade_id="other-1", order_id="0xother",
                    client_id=str(2**63 + 124))
    client = FakeClient({"": {"result": [external, external], "next": ""}})
    with pytest.raises(GrvtTestnetFillError, match="duplicado"):
        audit_testnet_fills(client, order(), now_ns=NOW_NS)
    client = FakeClient({"": {"result": [{**external, "fee": "NaN"}],
                              "next": ""}})
    with pytest.raises(GrvtTestnetFillError, match="Coste"):
        audit_testnet_fills(client, order(), now_ns=NOW_NS)


def test_history_cannot_start_after_order_creation():
    client = FakeClient({})
    with pytest.raises(GrvtTestnetFillError, match="Inicio"):
        audit_testnet_fills(client, order(), now_ns=NOW_NS,
                            start_time_ns=NOW_NS)
    assert client.calls == []
