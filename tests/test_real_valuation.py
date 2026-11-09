from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.portfolio.domain import EventInput, PortfolioEvent, fold_events
from app.portfolio.valuation import ReferencePrice, value_snapshot


NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
D = Decimal


def event(kind, *, asset=None, quantity=None, unit_price=None, amount=None):
    return PortfolioEvent(
        id=str(kind) + str(asset) + str(quantity), account_id="a", request_id="r",
        recorded_at=NOW,
        input=EventInput(kind, "EUR", NOW, asset, quantity, unit_price, amount),
    )


def test_fresh_reference_values_assets_without_counting_contribution_as_profit():
    book = fold_events("a", [
        event("CONTRIBUTION", amount=D("1000")),
        event("BUY", asset="BTC", quantity=D("1"), unit_price=D("100")),
    ])
    result = value_snapshot(book, {("BTC", "EUR"): ReferencePrice(D("120"), "kraken", NOW.isoformat(), False)}, now=NOW)
    assert result["currencies"][0]["equity"] == D("1020")
    assert result["currencies"][0]["unrealized_pnl"] == D("20")
    assert result["currencies"][0]["total_pnl"] == D("20")
    assert result["positions"][0]["unrealized_pnl"] == D("20")
    assert result["positions"][0]["price_source"] == "kraken"


def test_missing_or_stale_price_never_produces_complete_equity():
    book = fold_events("a", [
        event("CONTRIBUTION", amount=D("1000")),
        event("BUY", asset="BTC", quantity=D("1"), unit_price=D("100")),
    ])
    for quote in (
        None,
        ReferencePrice(D("120"), "manual", NOW.isoformat(), True),
        ReferencePrice(D("120"), "unknown", None, False),
        ReferencePrice(D("120"), "unknown", NOW.isoformat(), False),
        ReferencePrice(D("120"), "", NOW.isoformat(), False),
        ReferencePrice(D("120"), "kraken", "not-a-date", False),
        ReferencePrice(D("120"), "kraken", "2025-01-01T00:00:00", False),
    ):
        result = value_snapshot(book, {} if quote is None else {("BTC", "EUR"): quote}, now=NOW)
        assert result["currencies"][0]["equity"] is None
        assert result["currencies"][0]["total_pnl"] is None
        assert result["currencies"][0]["unrealized_pnl"] is None
        assert result["positions"][0]["unrealized_pnl"] is None
        assert result["currencies"][0]["reason"] == "missing_or_stale_price"
        assert result["currencies"][0]["allocation"] is None


def test_negative_cash_marks_history_incomplete():
    book = fold_events("a", [event("BUY", asset="BTC", quantity=D("1"), unit_price=D("100"))])
    result = value_snapshot(book, {("BTC", "EUR"): ReferencePrice(D("120"), "kraken", NOW.isoformat(), False)}, now=NOW)
    assert result["currencies"][0]["equity"] is None
    assert result["currencies"][0]["reason"] == "negative_cash_or_missing_contribution"
    assert result["currencies"][0]["allocation"] is None


def test_realized_result_survives_fully_closed_position_without_market_price():
    book = fold_events("a", [
        event("CONTRIBUTION", amount=D("1000")),
        event("BUY", asset="BTC", quantity=D("1"), unit_price=D("100")),
        event("SELL", asset="BTC", quantity=D("1"), unit_price=D("120")),
    ])
    result = value_snapshot(book, {})
    assert result["positions"] == []
    assert result["currencies"][0]["equity"] == D("1020")
    assert result["currencies"][0]["realized_pnl"] == D("20")
    assert result["currencies"][0]["unrealized_pnl"] == D("0")
    assert result["currencies"][0]["total_pnl"] == D("20")


def test_realized_result_remains_known_when_open_position_price_is_missing():
    book = fold_events("a", [
        event("CONTRIBUTION", amount=D("1000")),
        event("BUY", asset="BTC", quantity=D("2"), unit_price=D("100")),
        event("SELL", asset="BTC", quantity=D("1"), unit_price=D("120")),
    ])
    result = value_snapshot(book, {})
    assert result["currencies"][0]["equity"] is None
    assert result["currencies"][0]["reason"] == "missing_or_stale_price"
    assert result["currencies"][0]["realized_pnl"] == D("20")
    assert result["currencies"][0]["total_pnl"] is None


def test_missing_price_does_not_hide_missing_contribution():
    book = fold_events("a", [event("BUY", asset="BTC", quantity=D("1"), unit_price=D("100"))])
    result = value_snapshot(book, {})
    assert result["currencies"][0]["equity"] is None
    assert result["currencies"][0]["reason"] == "negative_cash_or_missing_contribution"
    assert result["currencies"][0]["total_pnl"] is None


def test_realized_and_unrealized_sum_without_treating_cash_flows_as_profit():
    book = fold_events("a", [
        event("CONTRIBUTION", amount=D("1000")),
        event("BUY", asset="BTC", quantity=D("2"), unit_price=D("100")),
        event("SELL", asset="BTC", quantity=D("1"), unit_price=D("120")),
        event("WITHDRAWAL", amount=D("500")),
        event("CONTRIBUTION", amount=D("500")),
    ])
    result = value_snapshot(book, {("BTC", "EUR"): ReferencePrice(D("130"), "kraken", NOW.isoformat(), False)}, now=NOW)
    [currency] = result["currencies"]
    assert currency["equity"] == D("1050")
    assert currency["realized_pnl"] == D("20")
    assert currency["unrealized_pnl"] == D("30")
    assert currency["total_pnl"] == D("50")
    assert book.contributions["EUR"] == D("1500")
    assert book.withdrawals["EUR"] == D("500")


def test_one_missing_price_hides_aggregate_result_without_hiding_known_positions():
    book = fold_events("a", [
        event("CONTRIBUTION", amount=D("1000")),
        event("BUY", asset="BTC", quantity=D("1"), unit_price=D("100")),
        event("BUY", asset="ETH", quantity=D("1"), unit_price=D("200")),
    ])
    result = value_snapshot(book, {("BTC", "EUR"): ReferencePrice(D("120"), "kraken", NOW.isoformat(), False)}, now=NOW)
    [currency] = result["currencies"]
    assert currency["complete"] is False
    assert currency["unrealized_pnl"] is None
    assert currency["total_pnl"] is None
    positions = {row["asset"]: row for row in result["positions"]}
    assert positions["BTC"]["unrealized_pnl"] == D("20")
    assert positions["ETH"]["unrealized_pnl"] is None


def test_real_valuation_rejects_out_of_window_price_even_if_provider_claims_fresh():
    book = fold_events("a", [
        event("CONTRIBUTION", amount=D("1000")),
        event("BUY", asset="BTC", quantity=D("1"), unit_price=D("100")),
    ])
    for seconds in (-301, 31):
        quote = ReferencePrice(D("120"), "kraken", (NOW + timedelta(seconds=seconds)).isoformat(), False)
        result = value_snapshot(book, {("BTC", "EUR"): quote}, now=NOW)
        assert result["currencies"][0]["equity"] is None
        assert result["currencies"][0]["reason"] == "missing_or_stale_price"
        assert result["positions"][0]["market_value"] is None
    for seconds in (-300, 30):
        quote = ReferencePrice(D("120"), "kraken", (NOW + timedelta(seconds=seconds)).isoformat(), False)
        result = value_snapshot(book, {("BTC", "EUR"): quote}, now=NOW)
        assert result["currencies"][0]["equity"] == D("1020")


def test_allocation_includes_cash_and_sums_to_100_percent_with_exact_remainders():
    events = [
        event("CONTRIBUTION", amount=D("3")),
        event("BUY", asset="ETH", quantity=D("1"), unit_price=D("1")),
        event("BUY", asset="BTC", quantity=D("1"), unit_price=D("1")),
    ]
    price = ReferencePrice(D("1"), "fixture", NOW.isoformat(), False)
    prices = {("ETH", "EUR"): price, ("BTC", "EUR"): price}
    [currency] = value_snapshot(fold_events("a", events), prices, now=NOW)["currencies"]
    assert currency["allocation"] == [
        {"kind":"ASSET", "asset":"BTC", "value":D("1"), "weight_bps":3334},
        {"kind":"ASSET", "asset":"ETH", "value":D("1"), "weight_bps":3333},
        {"kind":"CASH", "asset":None, "value":D("1"), "weight_bps":3333},
    ]
    assert currency["contributions"] == D("3") and currency["withdrawals"] == 0
    assert currency["total_pnl"] == 0


def test_cash_only_currency_and_zero_equity_are_handled_separately():
    from app.portfolio.domain import AccountSnapshot

    book = AccountSnapshot("a", cash={"EUR":D("0"), "USDT":D("10.01")}, withdrawals={"EUR":D("5")})
    currencies = {row["currency"]:row for row in value_snapshot(book, {}, now=NOW)["currencies"]}
    assert currencies["EUR"]["allocation"] == [] and currencies["EUR"]["withdrawals"] == D("5")
    assert currencies["USDT"]["allocation"] == [
        {"kind":"CASH", "asset":None, "value":D("10.01"), "weight_bps":10000},
    ]


def test_weight_rounding_keeps_the_exact_amount_of_a_small_position():
    book = fold_events("a", [
        event("CONTRIBUTION", amount=D("1000")),
        event("BUY", asset="BTC", quantity=D("0.00001"), unit_price=D("1")),
    ])
    quote = ReferencePrice(D("1"), "fixture", NOW.isoformat(), False)
    [currency] = value_snapshot(book, {("BTC", "EUR"):quote}, now=NOW)["currencies"]
    cash,asset = currency["allocation"]
    assert cash["weight_bps"] == 10000
    assert asset == {"kind":"ASSET", "asset":"BTC", "value":D("0.00001"), "weight_bps":0}
