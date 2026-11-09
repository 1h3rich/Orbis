"""Valoración privada informativa con precios públicos ficticios, sin red."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.pricing.mexc_preview import MexcPreviewError, preview_spot_valuation


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def balances(*rows, observed_at=NOW):
    return {"source": "MEXC_SPOT_PRIVATE_READONLY",
            "accounting": "OBSERVED_NOT_IMPORTED",
            "observed_at": observed_at.isoformat(),
            "balances": list(rows)}


def row(asset, free, locked="0"):
    total = Decimal(free) + Decimal(locked)
    return {"asset": asset, "free": free, "locked": locked, "total": format(total, "f")}


def quote(symbol, *, timestamp=NOW, bid="2500"):
    return {"symbol": symbol, "closeTime": int(timestamp.timestamp() * 1000),
            "bidPrice": bid, "askPrice": bid, "lastPrice": bid}


def test_complete_spot_preview_values_all_assets_and_locked_amounts():
    called = []

    def get_quote(symbol):
        called.append(symbol)
        return quote(symbol, bid={"BTCUSDT": "100000", "ETHUSDT": "2500"}[symbol])

    result = preview_spot_valuation(
        balances(row("BTC", "0.02", "0.001"), row("ETH", "1.5", "0.5"), row("USDT", "200", "10")),
        "USDT", clock=lambda: NOW + timedelta(seconds=1), quote_get=get_quote,
    )
    assert called == ["BTCUSDT", "ETHUSDT"]
    assert result["complete"] is True and result["unpriced_assets"] == []
    assert result["total_quote"] == "7310.000"
    assert result["accounting"] == "OBSERVED_NOT_IMPORTED"
    assert result["balances"][0]["value_quote"] == "2100.000"
    assert result["balances"][2]["value_quote"] == "210"


def test_missing_or_stale_price_never_publishes_a_partial_total():
    observed = balances(row("BTC", "0.02"), row("USDT", "200"))
    missing = preview_spot_valuation(observed, "USDT", clock=lambda: NOW,
                                     quote_get=lambda _: quote("ETHUSDT"))
    assert missing["complete"] is False
    assert missing["total_quote"] is None
    assert missing["unpriced_assets"] == ["BTC"]
    assert missing["balances"][1]["value_quote"] == "200"
    stale = preview_spot_valuation(observed, "USDT", clock=lambda: NOW,
                                   quote_get=lambda symbol: quote(symbol,
                                                                  timestamp=NOW - timedelta(seconds=31)))
    assert stale["total_quote"] is None
    assert stale["unpriced_assets"] == ["BTC"]


def test_preview_rejects_stale_private_data_and_inconsistent_totals():
    with pytest.raises(MexcPreviewError, match="caducados"):
        preview_spot_valuation(balances(row("BTC", "1"), observed_at=NOW - timedelta(seconds=31)),
                               "USDT", clock=lambda: NOW, quote_get=lambda _: quote("BTCUSDT"))
    bad = row("BTC", "1") | {"total": "2"}
    with pytest.raises(MexcPreviewError, match="no concuerda"):
        preview_spot_valuation(balances(bad), "USDT", clock=lambda: NOW,
                               quote_get=lambda _: quote("BTCUSDT"))
    with pytest.raises(MexcPreviewError, match="duplicado"):
        preview_spot_valuation(balances(row("BTC", "1"), row("BTC", "2")), "USDT",
                               clock=lambda: NOW, quote_get=lambda symbol: quote(symbol))


def test_preview_caps_quote_requests_without_claiming_completeness():
    assets = [row(f"T{i:02d}", "1") for i in range(26)]
    calls = []

    def get_quote(symbol):
        calls.append(symbol)
        return quote(symbol, bid="1")

    result = preview_spot_valuation(balances(*assets), "USDT", clock=lambda: NOW,
                                    quote_get=get_quote)
    assert len(calls) == 24
    assert result["complete"] is False and result["total_quote"] is None
    assert result["unpriced_assets"] == ["T24", "T25"]
