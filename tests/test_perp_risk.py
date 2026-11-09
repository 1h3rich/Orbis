"""Primitivas PAPER de perpetuos: no equivalen a liquidación real GRVT."""

from decimal import Decimal

import pytest

from app.derivatives.isolated import IsolatedPerpPosition
from app.derivatives.margin_threshold import estimate_maintenance_threshold
from app.derivatives.paper_math import MarginRules, MarginTier, PaperPerpError


def position(side: str = "LONG") -> IsolatedPerpPosition:
    return IsolatedPerpPosition(
        instrument="BTC_USDT_Perp",
        size=Decimal("0.1") if side == "LONG" else Decimal("-0.1"),
        entry_price=Decimal("50000"),
        collateral=Decimal("1000"),
        entry_fee=Decimal("2"),
    )


def margin_rules() -> MarginRules:
    return MarginRules("BTC_USDT_Perp", Decimal("10"), 1, (
        MarginTier(Decimal("0"), Decimal("5000"), Decimal("0.02"),
                   Decimal("0.01"), Decimal("0"), Decimal("50")),
        MarginTier(Decimal("5000"), None, Decimal("0.05"),
                   Decimal("0.025"), Decimal("75"), Decimal("20")),
    ))


def test_long_short_pnl_is_symmetric_and_fees_affect_equity():
    long = position()
    short = position("SHORT")
    assert long.side == "LONG" and short.side == "SHORT"
    assert long.unrealized_pnl(Decimal("51000")) == Decimal("100.0")
    assert short.unrealized_pnl(Decimal("51000")) == Decimal("-100.0")
    assert long.equity(Decimal("51000")) == Decimal("1098.0")
    assert short.equity(Decimal("51000")) == Decimal("898.0")


def test_open_margin_and_external_maintenance_trigger():
    long = position()
    assert long.entry_notional == Decimal("5000.0")
    assert long.initial_margin(Decimal("5")) == Decimal("1000.0")
    assert long.can_open(Decimal("5")) is False  # faltan 2 de fee
    assert long.can_open(Decimal("6")) is True
    assert long.margin_breached(Decimal("41000"), Decimal("100")) is True
    assert long.margin_breached(Decimal("42000"), Decimal("100")) is False


def test_signed_funding_is_ledgered_once_and_changes_risk():
    long = position()
    short = position("SHORT")
    rate = Decimal("0.001")
    assert long.funding_payment(Decimal("50000"), rate) == Decimal("5.0000")
    assert short.funding_payment(Decimal("50000"), rate) == Decimal("-5.0000")
    charged = long.with_funding(Decimal("5"))
    credited = short.with_funding(Decimal("-5"))
    assert charged.equity(Decimal("50000")) == Decimal("993.0")
    assert credited.equity(Decimal("50000")) == Decimal("1003.0")
    assert long.funding_paid == Decimal("0")  # inmutabilidad


def test_isolated_maintenance_threshold_uses_the_tier_at_the_crossing():
    rules = margin_rules()
    for side in ("LONG", "SHORT"):
        p = position(side)
        threshold = estimate_maintenance_threshold(p, rules, Decimal("50000"))
        assert threshold is not None
        assert (threshold < Decimal("50000")) == (side == "LONG")
        if side == "LONG":
            assert abs(p.size) * threshold < Decimal("5000")
        else:
            assert abs(p.size) * threshold > Decimal("5000")
        buffer = p.equity(threshold) - rules.maintenance_requirement(abs(p.size) * threshold)
        assert abs(buffer) < Decimal("1e-20")
        charged = p.with_funding(Decimal("10"))
        shifted = estimate_maintenance_threshold(charged, rules, Decimal("50000"))
        assert shifted is not None
        assert (shifted > threshold) == (side == "LONG")


def test_maintenance_threshold_fails_closed_if_not_identifiable():
    rules = margin_rules()
    assert estimate_maintenance_threshold(position(), rules, Decimal("40000")) is None
    fully_backed = IsolatedPerpPosition("BTC_USDT_Perp", Decimal("0.1"),
                                       Decimal("50000"), Decimal("6000"))
    assert estimate_maintenance_threshold(fully_backed, rules, Decimal("50000")) is None
    discontinuous = MarginRules("BTC_USDT_Perp", Decimal("10"), 1, (
        rules.tiers[0], MarginTier(Decimal("5000"), None, Decimal("0.05"),
                                  Decimal("0.025"), Decimal("0"), Decimal("20")),
    ))
    assert estimate_maintenance_threshold(position(), discontinuous, Decimal("50000")) is None
    with pytest.raises(PaperPerpError, match="distintos"):
        estimate_maintenance_threshold(position(), MarginRules("ETH_USDT_Perp", Decimal("10"),
                                                                1, rules.tiers), Decimal("50000"))


@pytest.mark.parametrize("field,value", [
    ("size", Decimal("0")), ("entry_price", Decimal("0")),
    ("collateral", Decimal("-1")), ("entry_fee", Decimal("NaN")),
])
def test_invalid_financial_inputs_fail_closed(field, value):
    params = {"instrument": "BTC_USDT_Perp", "size": Decimal("1"),
              "entry_price": Decimal("100"), "collateral": Decimal("100")}
    params[field] = value
    with pytest.raises(ValueError):
        IsolatedPerpPosition(**params)


def test_invalid_mark_and_leverage_fail_closed():
    p = position()
    with pytest.raises(ValueError):
        p.unrealized_pnl(Decimal("NaN"))
    with pytest.raises(ValueError):
        p.initial_margin(Decimal("0.5"))
    with pytest.raises(ValueError):
        p.margin_breached(Decimal("50000"), Decimal("-1"))
