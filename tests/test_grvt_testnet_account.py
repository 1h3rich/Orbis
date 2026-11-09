from decimal import Decimal

import pytest

from app.live_execution.grvt_testnet_account import (
    GrvtTestnetAccountError, audit_testnet_account,
)
from app.live_execution.grvt_testnet_fills import GrvtTestnetFillAudit
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderObservation
from app.live_execution.grvt_testnet_withdrawals import (
    GrvtTestnetWithdrawal, GrvtTestnetWithdrawalAudit,
)
from app.live_execution.grvt_testnet_interest import (
    GrvtTestnetInterestPayment, GrvtTestnetInterestAudit,
)


NOW = 1_780_000_000_000_000_000
SUB = "123456"
CID = str(2**63 + 123)


def observation():
    return GrvtTestnetOrderObservation(
        SUB, CID, "0x123abc", "FILLED", "BTC_USDT_Perp", "BUY",
        "0.01", "0.01", "50000", str(NOW - 1),
        create_time_ns=str(NOW - 10_000_000_000),
    )


def fills():
    return GrvtTestnetFillAudit(
        client_order_id=CID, order_id="0x123abc", fill_count=1,
        latest_fill_time_ns=NOW - 2, filled_size=Decimal("0.01"),
        latest_any_fill_time_ns=NOW - 2, history_start_ns=NOW - 10_000_000_000,
        order_traded_size=Decimal("0.01"), sizes_match=True,
        pages_drained=1, gross_notional=Decimal("500"),
        realized_pnl=Decimal("0"), builder_fee=Decimal("0"),
        fees_by_currency=(("USDT", Decimal("0.25")),),
        fills=(), other_fills=(),
    )


def account():
    return {
        "sub_account_id": SUB, "event_time": str(NOW),
        "settle_currency": "USDT", "total_equity": "9999.75",
        "initial_margin": "10", "maintenance_margin": "5",
        "spot_balances": [{"currency": "USDT", "balance": "9999.75"}],
        "positions": [{"sub_account_id": SUB,
                       "instrument": "BTC_USDT_Perp", "size": "0.01"}],
    }


class FakeClient:
    def __init__(self, account_payload=None, open_orders=None):
        self.account_payload = account() if account_payload is None else account_payload
        self.open_orders = [] if open_orders is None else open_orders
        self.calls = []

    def read_testnet(self, path, sub_account_id):
        self.calls.append((path, sub_account_id))
        return {"result": (self.account_payload if path == "account_summary"
                           else self.open_orders)}


def test_post_trade_snapshot_preserves_account_evidence_without_unlocking():
    client = FakeClient()
    snapshot = audit_testnet_account(client, observation(), fills(), now_ns=NOW)
    assert snapshot.settle_balance == Decimal("9999.75")
    assert snapshot.positions == (("BTC_USDT_Perp", Decimal("0.01")),)
    assert snapshot.initial_margin == Decimal("10")
    assert snapshot.open_order_count == 0
    assert snapshot.complete_for_execution is False
    assert client.calls == [("account_summary", SUB), ("open_orders", SUB)]


def test_snapshot_after_fill_but_before_order_update_is_not_post_order_evidence():
    # El fill llega en NOW-2; la actualización terminal de orden en NOW-1.
    # Una cuenta en NOW-2 no puede presentarse como snapshot posterior.
    stale = FakeClient({**account(), "event_time": str(NOW - 2)})
    with pytest.raises(GrvtTestnetAccountError, match="anterior a la última actividad"):
        audit_testnet_account(stale, observation(), fills(), now_ns=NOW)


@pytest.mark.parametrize("updated", ["", "no-time", str(NOW + 2_000_000_001)])
def test_order_update_time_must_be_valid_before_account_lookup(updated):
    order = observation()
    order = GrvtTestnetOrderObservation(
        order.sub_account_id, order.client_order_id, order.order_id,
        order.status, order.instrument, order.side, order.size,
        order.traded_size, order.limit_price, updated,
        create_time_ns=order.create_time_ns,
    )
    client = FakeClient()
    with pytest.raises(GrvtTestnetAccountError, match="Identidad"):
        audit_testnet_account(client, order, fills(), now_ns=NOW)
    assert client.calls == []


@pytest.mark.parametrize("change", [
    {"event_time": str(NOW - 3)},
    {"sub_account_id": "999"},
    {"total_equity": "NaN"},
    {"spot_balances": []},
    {"spot_balances": [{"currency": "USDT", "balance": "1"},
                       {"currency": "USDT", "balance": "2"}]},
    {"positions": [{"sub_account_id": "999",
                    "instrument": "BTC_USDT_Perp", "size": "0.01"}]},
])
def test_post_trade_snapshot_rejects_stale_cross_scope_or_invalid_state(change):
    client = FakeClient({**account(), **change})
    with pytest.raises(GrvtTestnetAccountError):
        audit_testnet_account(client, observation(), fills(), now_ns=NOW)


def test_terminal_order_still_open_is_contradictory():
    open_order = {"sub_account_id": SUB, "order_id": "0x123abc",
                  "state": {"status": "OPEN"}}
    with pytest.raises(GrvtTestnetAccountError, match="terminal"):
        audit_testnet_account(FakeClient(open_orders=[open_order]),
                              observation(), fills(), now_ns=NOW)


def test_other_open_order_is_reported_not_silently_ignored():
    open_order = {"sub_account_id": SUB, "order_id": "0xother",
                  "state": {"status": "OPEN"}}
    snapshot = audit_testnet_account(FakeClient(open_orders=[open_order]),
                                     observation(), fills(), now_ns=NOW)
    assert snapshot.open_order_count == 1
    assert snapshot.complete_for_execution is False


def test_snapshot_must_follow_a_relevant_withdrawal():
    withdrawal = GrvtTestnetWithdrawalAudit(
        SUB, NOW - 10_000, NOW + 1, 1,
        (GrvtTestnetWithdrawal("tx-1", NOW + 1, "USDT",
                                Decimal("1"), "PENDING"),),
        0, NOW + 1,
    )
    with pytest.raises(GrvtTestnetAccountError, match="anterior"):
        audit_testnet_account(FakeClient(), observation(), fills(),
                              now_ns=NOW + 1, withdrawals=withdrawal)


def test_snapshot_must_follow_an_interest_charge():
    charge = GrvtTestnetInterestAudit(
        SUB, NOW - 10_000, NOW + 1, 1,
        (GrvtTestnetInterestPayment(NOW + 1, NOW + 1, "USDT",
                                     Decimal("0.1"), Decimal("100")),),
        (("USDT", Decimal("0.1")),), NOW + 1,
    )
    with pytest.raises(GrvtTestnetAccountError, match="anterior"):
        audit_testnet_account(FakeClient(), observation(), fills(),
                              now_ns=NOW + 1, interest=charge)
