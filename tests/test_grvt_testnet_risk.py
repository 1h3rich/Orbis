from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from app.derivatives.paper_math import MarginRules
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits
from app.live_execution.grvt_testnet_risk import (
    MARGIN_RULES_URL, GrvtTestnetRiskError, GrvtTestnetRiskState,
    evaluate_testnet_risk, fetch_testnet_risk,
)
from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft


NOW = 1_780_000_000_000_000_000


def draft(*, side="BUY", reduce_only=False, size="0.01"):
    return GrvtTestnetLimitDraft(
        "123456", "BTC_USDT_Perp", side, Decimal(size), Decimal("50000"),
        reduce_only, "0x" + "a" * 40, str(2**63 + 3), 1, NOW + 60_000_000_000,
    )


def limits():
    return GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("1000"))


def rules():
    return MarginRules.from_payload({
        "instrument": "BTC_USDT_Perp", "max_position_size": "10",
        "fetched_at_ns": NOW,
        "risk_brackets": [{"tier": 1, "notional_floor": "0",
                           "notional_cap": None,
                           "maintenance_margin_rate": "0.01",
                           "initial_margin_rate": "0.02", "max_leverage": 50,
                           "cumulative_maintenance_amount": "0"}],
    }, "BTC_USDT_Perp", NOW)


def state():
    return GrvtTestnetRiskState(
        "123456", "USDT", NOW, NOW, "SIMPLE_CROSS_MARGIN",
        "SINGLE_ASSET_MODE", Decimal("10000"), Decimal("10000"), Decimal(0),
        Decimal(0), Decimal(0), (), 0,
    )


def test_conservative_testnet_open_and_reduce():
    allowed = evaluate_testnet_risk(draft(), limits(), state(), rules(), now_ns=NOW)
    assert allowed.allowed and allowed.reason == "CONSERVATIVE_TESTNET_PREFLIGHT"
    held = replace(state(), positions=(("BTC_USDT_Perp", Decimal("0.02")),),
                   initial_margin=Decimal("20"), maintenance_margin=Decimal("10"))
    reduced = evaluate_testnet_risk(draft(side="SELL", reduce_only=True),
                                    limits(), held, rules(), now_ns=NOW)
    assert reduced.allowed and reduced.mode == "REDUCE"
    assert not evaluate_testnet_risk(draft(side="BUY", reduce_only=True),
                                     limits(), held, rules(), now_ns=NOW).allowed


@pytest.mark.parametrize("reduce_only",[False,True])
def test_unexplained_equity_is_never_an_allowed_preflight(reduce_only):
    account = replace(state(),total_equity=Decimal("10000.1"))
    if reduce_only:
        account = replace(account,positions=(("BTC_USDT_Perp",Decimal("0.02")),),
                          initial_margin=Decimal("20"),maintenance_margin=Decimal("10"))
    decision = evaluate_testnet_risk(draft(side="SELL",reduce_only=reduce_only),
        limits(),account,rules(),now_ns=NOW)
    assert not decision.allowed and decision.reason == "EQUITY_PNL_MISMATCH"


@pytest.mark.parametrize("changed,reason", [
    (dict(total_equity=Decimal("999")), "EQUITY_BUFFER"),
    (dict(settle_balance=Decimal("999")), "SETTLEMENT_BUFFER"),
    (dict(open_order_count=1), "OPEN_ORDERS_PRESENT"),
    (dict(margin_type="PORTFOLIO_CROSS_MARGIN"), "UNSUPPORTED_MARGIN_MODE"),
    (dict(settle_currency="USDC"), "SETTLEMENT_CURRENCY_MISMATCH"),
    (dict(event_time_ns=NOW - 6_000_000_000), "STALE_RISK_DATA"),
    (dict(positions=(("ETH_USDT_Perp", Decimal("1")),)), "EXISTING_EXPOSURE"),
    (dict(initial_margin=Decimal("1")), "EXISTING_EXPOSURE"),
])
def test_preflight_blocks_unverifiable_or_risky_state(changed, reason):
    result = evaluate_testnet_risk(draft(), limits(), replace(state(), **changed),
                                   rules(), now_ns=NOW)
    assert not result.allowed and result.reason == reason


@pytest.mark.parametrize('direct', [True, False])
def test_fetch_reads_only_testnet_and_preserves_account_freshness(direct):
    calls = []
    class FakeClient:
        def read_testnet(self, path, sub_id):
            calls.append((path, sub_id))
            if path == "account_summary":
                return {"result": {"sub_account_id": sub_id,
                        "event_time": str(NOW), "settle_currency": "USDT",
                        "margin_type": "SIMPLE_CROSS_MARGIN",
                        "sub_account_mode": "SINGLE_ASSET_MODE",
                        "total_equity": "10000", "initial_margin": "0",
                        "maintenance_margin": "0", "unrealized_pnl": "0",
                        "positions": [],
                        "spot_balances": [{"currency": "USDT", "balance": "10000"}]}}
            return {"result": []}

    def public_post(url, body):
        calls.append((url, body))
        payload = {
            "instrument": "BTC_USDT_Perp", "max_position_size": "10",
            "risk_brackets": [{"tier": 1, "notional_floor": "0",
                               "notional_cap": None,
                               "maintenance_margin_rate": "0.01",
                               "initial_margin_rate": "0.02", "max_leverage": 50,
                               "cumulative_maintenance_amount": "0"}],
        }
        return httpx.Response(200, json=payload if direct else {"result": payload},
                              request=httpx.Request("POST", url))

    fetched, margin_rules = fetch_testnet_risk(
        FakeClient(), "BTC_USDT_Perp", "123456",
        public_post=public_post, clock_ns=lambda: NOW,
    )
    assert fetched == state()
    assert margin_rules.instrument == "BTC_USDT_Perp"
    assert calls == [("account_summary", "123456"), ("open_orders", "123456"),
                     (MARGIN_RULES_URL, {"instrument": "BTC_USDT_Perp"})]


def test_fetch_rejects_cross_subaccount_and_bad_rules():
    class BadClient:
        def read_testnet(self, path, sub_id):
            if path == "account_summary":
                return {"result": {"sub_account_id": "999", "positions": []}}
            return {"result": []}
    def public_post(url, body):
        return httpx.Response(200, json={"result": {"instrument": "BTC_USDT_Perp"}},
                              request=httpx.Request("POST", url))
    with pytest.raises(GrvtTestnetRiskError, match="no verificable"):
        fetch_testnet_risk(BadClient(), "BTC_USDT_Perp", "123456",
                           public_post=public_post, clock_ns=lambda: NOW)


@pytest.mark.parametrize("foreign_balance,rejected", [("0.01", True), ("0", False)])
def test_single_asset_testnet_rejects_other_nonzero_collateral(foreign_balance, rejected):
    class MixedBalanceClient:
        def read_testnet(self, path, sub_id):
            if path == "account_summary":
                return {"result": {"sub_account_id": sub_id,
                        "event_time": str(NOW), "settle_currency": "USDT",
                        "margin_type": "SIMPLE_CROSS_MARGIN",
                        "sub_account_mode": "SINGLE_ASSET_MODE",
                        "total_equity": "10000", "initial_margin": "0",
                        "maintenance_margin": "0", "unrealized_pnl": "0",
                        "positions": [], "spot_balances": [
                            {"currency": "USDT", "balance": "10000"},
                            {"currency": "ETH", "balance": foreign_balance},
                        ]}}
            return {"result": []}

    def public_post(url, body):
        return httpx.Response(200, json={"result": {
            "instrument": "BTC_USDT_Perp", "max_position_size": "10",
            "risk_brackets": [{"tier": 1, "notional_floor": "0",
                               "notional_cap": None,
                               "maintenance_margin_rate": "0.01",
                               "initial_margin_rate": "0.02", "max_leverage": 50,
                               "cumulative_maintenance_amount": "0"}],
        }}, request=httpx.Request("POST", url))

    if rejected:
        with pytest.raises(GrvtTestnetRiskError, match="no verificable"):
            fetch_testnet_risk(MixedBalanceClient(), "BTC_USDT_Perp", "123456",
                               public_post=public_post, clock_ns=lambda: NOW)
    else:
        fetched, _ = fetch_testnet_risk(
            MixedBalanceClient(), "BTC_USDT_Perp", "123456",
            public_post=public_post, clock_ns=lambda: NOW,
        )
        assert fetched.settle_balance == Decimal("10000")
