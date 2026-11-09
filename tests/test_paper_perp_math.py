"""Contratos exactos de precio, frescura y riesgo PAPER de perpetuos."""

from decimal import Decimal

import pytest

from app.derivatives.paper_math import InstrumentSpec, MarginRules, PaperPerpError, PerpQuote, decimal_string


NOW = 1_800_000_000_000_000_000
INSTRUMENT = "BTC_USDT_Perp"


def ticker(**changes):
    value = {"instrument": INSTRUMENT, "event_time_ns": NOW,
             "mark_price": "100", "bid": "99", "ask": "101"}
    value.update(changes)
    return value


def rules(**changes):
    value = {"instrument": INSTRUMENT, "fetched_at_ns": NOW,
             "max_position_size": "10", "risk_brackets": [
                 {"tier": 1, "notional_floor": "0", "notional_cap": "1000",
                  "initial_margin_rate": "0.02", "maintenance_margin_rate": "0.01",
                  "cumulative_maintenance_amount": "0",
                  "max_leverage": 50},
                 {"tier": 2, "notional_floor": "1000", "notional_cap": None,
                  "initial_margin_rate": "0.05", "maintenance_margin_rate": "0.025",
                  "cumulative_maintenance_amount": "15",
                  "max_leverage": 20},
             ]}
    value.update(changes)
    return value


def instrument_spec(**changes):
    value = {"instrument": INSTRUMENT, "fetched_at_ns": NOW, "kind": "PERPETUAL",
             "venues": ["ORDERBOOK", "RFQ"], "base_decimals": 9,
             "tick_size": "0.1", "min_size": "0.001",
             "min_notional": "100", "max_position_size": "1000"}
    value.update(changes)
    return value


@pytest.mark.parametrize("value", [1, 0.1, True, None, "NaN", "Infinity", "1e19", "1e-1000000", "0e1000000", "", "x"])
def test_financial_inputs_require_bounded_finite_decimal_text(value):
    with pytest.raises(PaperPerpError):
        decimal_string(value, "cantidad")


@pytest.mark.parametrize("changes", [
    {"instrument": "ETH_USDT_Perp"},
    {"event_time_ns": NOW - 15_000_000_001},
    {"event_time_ns": NOW + 2_000_000_001},
    {"event_time_ns": "mal"},
    {"event_time_ns": float(NOW)},
    {"event_time_ns": True},
    {"event_time_ns": str(NOW) + ".5"},
    {"event_time_ns": " " + str(NOW)},
    {"bid": "102"},
    {"ask": "NaN"},
    {"mark_price": "0"},
])
def test_quote_rejects_wrong_stale_future_or_inconsistent_data(changes):
    with pytest.raises(PaperPerpError):
        PerpQuote.from_ticker(ticker(**changes), INSTRUMENT, NOW)


def test_quote_accepts_freshness_boundary_and_applies_adverse_slippage():
    quote = PerpQuote.from_ticker(ticker(event_time_ns=NOW - 15_000_000_000), INSTRUMENT, NOW)
    bps = Decimal("10")
    assert quote.fill_price("LONG", "OPEN", bps) == Decimal("101.101")
    assert quote.fill_price("LONG", "CLOSE", bps) == Decimal("98.901")
    assert quote.fill_price("SHORT", "OPEN", bps) == Decimal("98.901")
    assert quote.fill_price("SHORT", "CLOSE", bps) == Decimal("101.101")
    with pytest.raises(PaperPerpError):
        quote.fill_price("LONG", "OPEN", Decimal("501"))


def test_direct_quote_normalizes_exact_integer_prices():
    quote = PerpQuote(INSTRUMENT, NOW, 100, 99, 101)
    assert (quote.mark, quote.bid, quote.ask) == (Decimal("100"), Decimal("99"), Decimal("101"))
    assert all(type(price) is Decimal for price in (quote.mark, quote.bid, quote.ask))
    assert quote.fill_price("LONG", "OPEN", Decimal("10")) == Decimal("101.101")


@pytest.mark.parametrize("prices", [
    (100.1, 99, 101), (100, 99.1, 101), (100, 99, 101.1),
    (True, 99, 101), (Decimal("NaN"), 99, 101),
    (Decimal("Infinity"), 99, 101), (0, 99, 101), (100, 102, 101),
])
def test_direct_quote_rejects_unsafe_prices(prices):
    with pytest.raises(PaperPerpError):
        PerpQuote(INSTRUMENT, NOW, *prices)


@pytest.mark.parametrize("timestamp", [0, -1, 1.5, True])
def test_direct_quote_requires_exact_positive_event_timestamp(timestamp):
    with pytest.raises(PaperPerpError):
        PerpQuote(INSTRUMENT, timestamp, 100, 99, 101)


@pytest.mark.parametrize("changes", [
    {"instrument": "ETH_USDT_Perp"},
    {"fetched_at_ns": NOW - 60_000_000_001},
    {"fetched_at_ns": NOW + 2_000_000_001},
    {"risk_brackets": []},
    {"max_position_size": "0"},
    {"risk_brackets": [{"tier": 1, "notional_floor": "1", "notional_cap": None,
                        "initial_margin_rate": "0.02", "maintenance_margin_rate": "0.01",
                        "max_leverage": 50}]},
    {"risk_brackets": [{"tier": 1, "notional_floor": "0", "notional_cap": None,
                        "initial_margin_rate": "0.01", "maintenance_margin_rate": "0.02",
                        "max_leverage": 50}]},
])
def test_margin_rules_reject_missing_stale_or_unsafe_tiers(changes):
    with pytest.raises(PaperPerpError):
        MarginRules.from_payload(rules(**changes), INSTRUMENT, NOW)


def test_margin_tier_boundary_uses_next_tier_and_rejects_gap():
    parsed = MarginRules.from_payload(rules(), INSTRUMENT, NOW)
    assert parsed.tier_for(Decimal("999.999")).max_leverage == Decimal("50")
    assert parsed.tier_for(Decimal("1000")).max_leverage == Decimal("20")
    assert parsed.maintenance_requirement(Decimal("999")) == Decimal("9.99")
    assert parsed.maintenance_requirement(Decimal("1000")) == Decimal("10")
    assert parsed.maintenance_requirement(Decimal("1200")) == Decimal("15")
    with pytest.raises(PaperPerpError):
        parsed.tier_for(Decimal("0"))
    gapped = rules()
    gapped["risk_brackets"][1]["notional_floor"] = "1001"
    with pytest.raises(PaperPerpError):
        MarginRules.from_payload(gapped, INSTRUMENT, NOW)


@pytest.mark.parametrize("amount", [None, "-1", "NaN", "100"])
def test_maintenance_amount_fails_closed_when_missing_or_incoherent(amount):
    payload = rules()
    if amount is None:
        del payload["risk_brackets"][0]["cumulative_maintenance_amount"]
    else:
        payload["risk_brackets"][0]["cumulative_maintenance_amount"] = amount
    with pytest.raises(PaperPerpError):
        MarginRules.from_payload(payload, INSTRUMENT, NOW)


def test_instrument_minimum_step_and_notional_are_distinct():
    spec = InstrumentSpec.from_payload(instrument_spec(), INSTRUMENT, NOW)
    assert spec.size_step == Decimal("0.000000001")
    assert spec.min_size == Decimal("0.001")
    spec.validate_open(Decimal("0.001"), Decimal("100000"))
    assert spec.json()["min_notional"] == "100"
    with pytest.raises(PaperPerpError, match="mínimo GRVT"):
        spec.validate_open(Decimal("0.000999999"), Decimal("100000"))
    with pytest.raises(PaperPerpError, match="múltiplo"):
        spec.validate_open(Decimal("0.0010000005"), Decimal("100000"))
    with pytest.raises(PaperPerpError, match="Nocional inferior"):
        spec.validate_open(Decimal("0.001"), Decimal("99999.99"))
    with pytest.raises(PaperPerpError, match="máximo GRVT"):
        spec.validate_open(Decimal("1000.001"), Decimal("100000"))


@pytest.mark.parametrize("changes", [
    {"instrument": "ETH_USDT_Perp"},
    {"kind": "FUTURE"},
    {"venues": ["RFQ"]},
    {"fetched_at_ns": NOW - 60_000_000_001},
    {"fetched_at_ns": NOW + 2_000_000_001},
    {"base_decimals": True},
    {"base_decimals": 1000000},
    {"min_size": "0"},
    {"min_size": "0.0000000005"},
    {"min_size": "0.0010000005"},
    {"min_notional": "NaN"},
    {"tick_size": "0"},
    {"max_position_size": "0.0001"},
])
def test_instrument_spec_fails_closed_on_incoherent_public_rules(changes):
    with pytest.raises(PaperPerpError):
        InstrumentSpec.from_payload(instrument_spec(**changes), INSTRUMENT, NOW)
