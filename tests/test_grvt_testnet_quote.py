"""Precio IOC GRVT: sólo ticker testnet reciente y límites explícitos."""

from decimal import Decimal

import httpx
import pytest

from app.live_execution.grvt_testnet_quote import (
    TESTNET_TICKER_URL, GrvtTestnetQuoteError,
    fetch_testnet_quote, guard_testnet_limit_price,
)


NOW = 1_780_000_000_000_000_000


def ticker(**changes):
    row = {
        "instrument": "BTC_USDT_Perp", "event_time": str(NOW),
        "best_bid_price": "49999", "best_ask_price": "50000",
        "best_bid_size": "1.5", "best_ask_size": "2.5",
    }
    row.update(changes)
    return row


def fetch(row):
    calls = []

    def post(url, body):
        calls.append((url, body))
        return httpx.Response(200, json={"result": row},
                              request=httpx.Request("POST", url))

    quote = fetch_testnet_quote("BTC_USDT_Perp", http_post=post,
                                clock_ns=lambda: NOW)
    assert calls == [(TESTNET_TICKER_URL, {"instrument": "BTC_USDT_Perp"})]
    return quote


def test_quote_uses_only_fresh_testnet_bid_ask():
    quote = fetch(ticker())
    assert quote.bid == Decimal("49999")
    assert quote.ask == Decimal("50000")
    assert quote.event_time_ns == NOW
    guard_testnet_limit_price("BUY", Decimal("50000"), quote,
                              max_spread_bps=100, max_slippage_bps=50, now_ns=NOW)
    guard_testnet_limit_price("SELL", Decimal("49999"), quote,
                              max_spread_bps=100, max_slippage_bps=50, now_ns=NOW)


@pytest.mark.parametrize("change", [
    {"instrument": "ETH_USDT_Perp"},
    {"event_time": str(NOW - 6_000_000_000)},
    {"event_time": str(NOW + 2_000_000_000)},
    {"best_bid_price": "50001"},
    {"best_ask_price": None},
    {"best_bid_size": "0"},
    {"best_ask_size": "NaN"},
])
def test_incomplete_stale_or_crossed_ticker_fails_closed(change):
    with pytest.raises(GrvtTestnetQuoteError, match="no verificable"):
        fetch(ticker(**change))


def test_price_guard_rejects_worse_than_authorized_slippage():
    quote = fetch(ticker())
    with pytest.raises(GrvtTestnetQuoteError, match="compra"):
        guard_testnet_limit_price("BUY", Decimal("50501"), quote,
                                  max_spread_bps=100, max_slippage_bps=100, now_ns=NOW)
    with pytest.raises(GrvtTestnetQuoteError, match="venta"):
        guard_testnet_limit_price("SELL", Decimal("49498"), quote,
                                  max_spread_bps=100, max_slippage_bps=100, now_ns=NOW)
    with pytest.raises(GrvtTestnetQuoteError, match="Spread"):
        guard_testnet_limit_price("BUY", Decimal("50000"), quote,
                                  max_spread_bps=0, max_slippage_bps=100, now_ns=NOW)
    with pytest.raises(GrvtTestnetQuoteError, match="Política"):
        guard_testnet_limit_price("BUY", Decimal("50000"), quote,
                                  max_spread_bps=100, max_slippage_bps=101, now_ns=NOW)
    with pytest.raises(GrvtTestnetQuoteError, match="Política"):
        guard_testnet_limit_price("BUY", Decimal("50000"), quote,
                                  max_spread_bps=100, max_slippage_bps=50,
                                  now_ns=NOW + 6_000_000_000)
