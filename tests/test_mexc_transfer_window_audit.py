"""La auditoría de traslados sólo usa instantáneas y claves ficticias."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.live_execution.mexc_transfer_window_audit import (
    audit_spot_transfer_window, observe_and_audit_spot_transfers,
)
from app.live_execution.mexc_uid_guard import claim_mexc_uid_in_transaction
from app.live_execution.policy import LiveSpotPolicy
from app.portfolio.vault import RealVault


NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
ACCOUNT = "a" * 64
UID = "123456789"


def policy():
    return LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=30, expires_at=NOW + timedelta(hours=1), enabled=True,
    )


def window(*, deposits=(), withdrawals=(), end=NOW, observed=NOW):
    return {
        "source": "MEXC_SPOT_PRIVATE_READONLY",
        "accounting": "OBSERVED_NOT_IMPORTED",
        "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
        "start_ms": int((NOW - timedelta(days=1)).timestamp() * 1000),
        "end_ms": int(end.timestamp() * 1000),
        "observed_at": observed.isoformat(),
        "deposits": list(deposits), "withdrawals": list(withdrawals),
    }


def movement(direction, *, status=5):
    return {"direction": direction, "asset": "BTC", "network": "BTC",
            "amount": "0.01", "fee": "0", "status": status,
            "time_ms": int((NOW - timedelta(minutes=1)).timestamp() * 1000)}


def test_empty_window_is_only_a_scoped_observation():
    result = audit_spot_transfer_window(policy(), window(), now=NOW)
    assert result.clear_for_window
    assert result.reason == "NO_TRANSFERS_IN_OBSERVED_WINDOW"
    assert result.deposit_count == result.withdrawal_count == 0
    assert not hasattr(result, "reconciled")


@pytest.mark.parametrize("field,direction,status", [
    ("deposits", "deposit", 5),
    ("deposits", "deposit", 0),
    ("withdrawals", "withdrawal", 7),
    ("withdrawals", "withdrawal", 1),
])
def test_any_observed_external_movement_blocks_window(field, direction, status):
    result = audit_spot_transfer_window(policy(), window(**{
        field: [movement(direction, status=status)],
    }), now=NOW)
    assert not result.clear_for_window
    assert result.reason == "TRANSFER_ACTIVITY_REQUIRES_RECONCILIATION"
    assert result.deposit_count + result.withdrawal_count == 1


def test_stale_incomplete_or_malformed_window_fails_closed():
    active = policy()
    assert audit_spot_transfer_window(active, window(
        end=NOW - timedelta(seconds=31)), now=NOW).reason == "TRANSFER_WINDOW_STALE"
    assert audit_spot_transfer_window(active, window(
        observed=NOW - timedelta(seconds=31)), now=NOW).reason == "TRANSFER_WINDOW_STALE"
    assert audit_spot_transfer_window(active, window() | {
        "coverage": "PAGE_ONLY"}, now=NOW).reason == "TRANSFER_WINDOW_INVALID"
    assert audit_spot_transfer_window(active, window() | {
        "deposits": [movement("deposit") | {"amount": "NaN"}]},
        now=NOW).reason == "TRANSFER_WINDOW_INVALID"


class FakeView:
    def __init__(self, *, uid=UID, snapshot=None, unavailable=False):
        self.uid = uid
        self.snapshot = snapshot if snapshot is not None else window()
        self.unavailable = unavailable
        self.calls = []

    def account_reference(self):
        self.calls.append("account_reference")
        return ACCOUNT

    def account_uid(self):
        self.calls.append("account_uid")
        return self.uid

    def transfers_recent(self, *, days):
        self.calls.append(("transfers_recent", days))
        if self.unavailable:
            raise RuntimeError("private provider detail")
        return self.snapshot


def test_private_observer_requires_known_stable_uid_and_complete_query(tmp_path):
    vault = RealVault(tmp_path / "isolated-real.db")
    vault.unlock("isolated transfer audit password", create=True)
    view = FakeView()
    result = observe_and_audit_spot_transfers(vault, view, policy(), days=1, now=NOW)
    assert result.reason == "VIEW_UID_UNVERIFIED"
    assert view.calls == ["account_reference", "account_uid"]

    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        claim_mexc_uid_in_transaction(db, ACCOUNT, UID, now=NOW)
        db.commit()
    result = observe_and_audit_spot_transfers(vault, view, policy(), days=1, now=NOW)
    assert result.clear_for_window
    assert view.calls[-3:] == ["account_uid", ("transfers_recent", 1), "account_uid"]

    class ChangingUidView(FakeView):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def account_uid(self):
            self.reads += 1
            return UID if self.reads == 1 else "987654321"

    changed = observe_and_audit_spot_transfers(
        vault, ChangingUidView(), policy(), days=1, now=NOW,
    )
    assert changed.reason == "VIEW_UID_CHANGED" and not changed.clear_for_window

    view.unavailable = True
    result = observe_and_audit_spot_transfers(vault, view, policy(), days=1, now=NOW)
    assert not result.clear_for_window and result.reason == "VIEW_UNAVAILABLE"
    assert "private provider detail" not in repr(result)
    vault.lock()
