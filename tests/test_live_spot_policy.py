"""La autorización provisional REAL debe fallar cerrada ante cualquier duda."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from app.live_execution.policy import (
    LiveSpotIntent,
    LiveSpotPolicy,
    LiveSpotState,
    evaluate_spot_intent,
)


NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "a" * 64
POLICY_ID = "651a674d-3427-40a7-94cc-290168123310"
INTENT_ID = "812a529b-398a-438f-9050-7a62bba71711"


def valid_case():
    policy = LiveSpotPolicy(
        policy_id=POLICY_ID,
        strategy_version_id="strategy-v1",
        account_ref=ACCOUNT,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        max_order_quote=Decimal("150"),
        max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"),
        max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"),
        max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60,
        expires_at=NOW + timedelta(hours=1),
        enabled=True,
    )
    intent = LiveSpotIntent(
        intent_id=INTENT_ID,
        policy_id=POLICY_ID,
        strategy_version_id="strategy-v1",
        account_ref=ACCOUNT,
        symbol="BTCUSDT",
        side="BUY",
        quantity_base=Decimal("0.001"),
        limit_price_quote=Decimal("100000"),
        estimated_fee_quote=Decimal("0.1"),
        signal_at=NOW - timedelta(seconds=5),
    )
    state = LiveSpotState(
        account_ref=ACCOUNT,
        symbol="BTCUSDT",
        reconciled=True,
        stopped=False,
        account_verified=True,
        instrument_verified=True,
        observed_at=NOW - timedelta(seconds=3),
        quote_at=NOW - timedelta(seconds=2),
        best_bid=Decimal("99990"),
        best_ask=Decimal("100000"),
        free_base=Decimal("0.01"),
        free_quote=Decimal("200"),
        pending_base=Decimal("0"),
        pending_quote=Decimal("0"),
        daily_turnover_quote=Decimal("100"),
        pending_turnover_quote=Decimal("0"),
        exposure_quote=Decimal("100"),
        daily_realized_pnl_quote=Decimal("-10"),
        average_cost_quote_per_base=Decimal("100000"),
    )
    return policy, intent, state


def decide(*, policy=None, intent=None, state=None):
    base_policy, base_intent, base_state = valid_case()
    return evaluate_spot_intent(
        policy or base_policy, intent or base_intent, state or base_state, now=NOW
    )


def test_buy_authorization_is_provisional_and_exact_decimal():
    result = decide()
    assert result.allowed is True
    assert result.reason == "AUTHORIZED_FOR_RESERVATION"
    assert result.notional_quote == Decimal("100.000")
    assert result.reserved_quote == Decimal("100.100")


def test_buy_reservation_includes_tiny_positive_fee_without_rounding():
    policy, intent, state = valid_case()
    tiny_fee = Decimal("1e-100")
    result = evaluate_spot_intent(
        policy, replace(intent, estimated_fee_quote=tiny_fee),
        replace(state, free_quote=Decimal("100")), now=NOW,
    )
    assert result.reason == "INSUFFICIENT_QUOTE"
    assert result.reserved_quote > Decimal("100")


def test_sell_uses_available_base_and_price_bound():
    policy, intent, state = valid_case()
    sell = replace(intent, side="SELL")
    assert decide(intent=sell).allowed
    assert decide(intent=sell).reserved_quote == 0
    assert decide(state=replace(state, free_base=Decimal("0.001")), intent=sell).reason == "INSUFFICIENT_BASE"
    assert decide(state=replace(state, free_base=Decimal("0.0005")), intent=sell).reason == "INSUFFICIENT_BASE"
    assert decide(intent=replace(sell, limit_price_quote=Decimal("99000"))).reason == "PRICE_BOUND"
    assert decide(state=replace(state, pending_base=Decimal("0.01")), intent=sell).reason == "INSUFFICIENT_BASE"


def test_sell_requires_cost_basis_and_reserves_projected_loss_including_fee():
    policy, intent, state = valid_case()
    sell = replace(intent, side="SELL", limit_price_quote=Decimal("100000"))
    assert decide(intent=sell, state=replace(state, average_cost_quote_per_base=None)).reason == "SELL_COST_BASIS_UNKNOWN"
    # Con comisión potencial en base, el coste de esa comisión puede superar
    # 0.1 USDT; no se acepta una venta que parecería segura sólo en quote.
    at_limit = replace(state, average_cost_quote_per_base=Decimal("139900"))
    assert decide(intent=sell, state=at_limit).reason == "PROJECTED_DAILY_LOSS_LIMIT"
    assert decide(intent=sell, state=replace(at_limit, average_cost_quote_per_base=Decimal("139800"))).allowed
    assert decide(intent=sell, state=replace(state, pending_projected_loss_quote=Decimal("39.9"))).reason == "PROJECTED_DAILY_LOSS_LIMIT"


def test_sell_cost_basis_must_be_finite_nonnegative_decimal():
    _, _, state = valid_case()
    for bad in (float("nan"), Decimal("NaN"), Decimal("-1")):
        with pytest.raises(ValueError):
            replace(state, average_cost_quote_per_base=bad)


def test_derived_cost_and_pnl_keep_book_precision_without_relaxing_external_inputs():
    policy, intent, state = valid_case()
    with localcontext() as context:
        context.prec = 300
        average = Decimal("100000") / 3
        realized = -Decimal(1) / 7
    derived = replace(state, average_cost_quote_per_base=average, daily_realized_pnl_quote=realized)
    assert derived.average_cost_quote_per_base == average and derived.daily_realized_pnl_quote == realized
    assert decide(state=derived).allowed
    with pytest.raises(ValueError): replace(intent, limit_price_quote=average)
    with pytest.raises(ValueError): replace(policy, max_order_quote=average)
    with pytest.raises(ValueError): replace(state, free_quote=average)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"enabled": False}, "POLICY_DISABLED"),
        ({"expires_at": NOW}, "POLICY_OR_SIGNAL_TIME_INVALID"),
        ({"strategy_version_id": "other"}, "SCOPE_MISMATCH"),
        ({"symbol": "ETHUSDT", "base_asset": "ETH"}, "SCOPE_MISMATCH"),
        ({"max_order_quote": Decimal("99")}, "ORDER_LIMIT"),
        ({"max_fee_bps": Decimal("9")}, "FEE_LIMIT"),
        ({"max_daily_turnover_quote": Decimal("199")}, "DAILY_TURNOVER_LIMIT"),
        ({"max_daily_loss_quote": Decimal("10")}, "DAILY_LOSS_LIMIT"),
        ({"max_exposure_quote": Decimal("199")}, "EXPOSURE_LIMIT"),
        ({"max_slippage_bps": Decimal("0")}, "AUTHORIZED_FOR_RESERVATION"),
    ],
)
def test_policy_limits(change, reason):
    policy, _, _ = valid_case()
    assert decide(policy=replace(policy, **change)).reason == reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"stopped": True}, "STOPPED"),
        ({"account_verified": False}, "UNVERIFIED_ACCOUNT"),
        ({"instrument_verified": False}, "UNVERIFIED_INSTRUMENT"),
        ({"reconciled": False}, "NOT_RECONCILED"),
        ({"observed_at": NOW - timedelta(seconds=61)}, "STALE_DATA_OR_SIGNAL"),
        ({"quote_at": NOW - timedelta(seconds=61)}, "STALE_DATA_OR_SIGNAL"),
        ({"free_quote": Decimal("100")}, "INSUFFICIENT_QUOTE"),
        ({"pending_quote": Decimal("100")}, "INSUFFICIENT_QUOTE"),
        ({"pending_turnover_quote": Decimal("301")}, "DAILY_TURNOVER_LIMIT"),
        ({"best_bid": Decimal("99000"), "best_ask": Decimal("99000")}, "PRICE_BOUND"),
    ],
)
def test_state_guards(change, reason):
    _, _, state = valid_case()
    assert decide(state=replace(state, **change)).reason == reason


def test_stale_or_future_signal_is_denied():
    _, intent, _ = valid_case()
    assert decide(intent=replace(intent, signal_at=NOW - timedelta(seconds=61))).reason == "STALE_DATA_OR_SIGNAL"
    assert decide(intent=replace(intent, signal_at=NOW + timedelta(seconds=1))).reason == "POLICY_OR_SIGNAL_TIME_INVALID"


@pytest.mark.parametrize("bad", [float("nan"), 1.2, Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
def test_invalid_money_is_rejected(bad):
    _, _, state = valid_case()
    with pytest.raises(ValueError):
        replace(state, free_quote=bad)


@pytest.mark.parametrize("bad", [float("nan"), 1.2, Decimal("NaN"), Decimal("Infinity")])
def test_invalid_signed_pnl_is_rejected(bad):
    _, _, state = valid_case()
    with pytest.raises(ValueError):
        replace(state, daily_realized_pnl_quote=bad)


def test_timezone_and_reference_are_required():
    policy, _, state = valid_case()
    with pytest.raises(ValueError):
        replace(policy, expires_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        replace(state, account_ref="my-wallet")
