"""Historia anterior exclusiva: no pedir datos posteriores ni mezclar fuentes."""

import pytest
from urllib.parse import parse_qs, urlparse

from app.marketdata import service
from app.marketdata.adapters.binance import BinanceAdapter
from app.marketdata.adapters.bitstamp import BitstampAdapter
from app.marketdata.adapters.coinbase import CoinbaseAdapter
from app.marketdata.adapters.mexc import MexcAdapter


BOUND = 1_700_000_000


@pytest.mark.parametrize("adapter,symbol,timeframe,url_part,required_start", [
    (BinanceAdapter, "BTC/USDT", "1h", f"endTime={BOUND * 1000 - 1}", None),
    (MexcAdapter, "BTC/USDT", "1h", f"endTime={BOUND * 1000 - 1}",
     f"startTime={(BOUND - 200 * 3600) * 1000}"),
    (BitstampAdapter, "BTC/USD", "1d", f"end={BOUND - 1}", None),
])
def test_older_adapters_send_exclusive_end_not_forward_start(
    adapter, symbol, timeframe, url_part, required_start,
):
    urls = []

    def fetch(url, timeout=5.0):
        urls.append(url)
        if adapter is BitstampAdapter:
            return {"data": {"ohlc": []}}
        return []

    adapter(http_get=fetch).get_candles_before(
        symbol, timeframe=timeframe, limit=200, before=BOUND,
    )
    assert len(urls) == 1
    assert url_part in urls[0]
    if required_start:
        assert required_start in urls[0]
    else:
        assert "startTime=" not in urls[0] and "start=" not in urls[0]


def test_coinbase_older_page_uses_bounded_range_and_excludes_boundary():
    urls = []

    def fetch(url, timeout=5.0):
        urls.append(url)
        return [
            [BOUND, 9, 11, 10, 10, 1],
            [BOUND - 3600, 9, 11, 10, 10, 1],
        ]

    result = service.candles("BTC/USD", provider="coinbase", timeframe="1h",
                             limit=1000, before=BOUND, http_get=fetch)
    assert result["available"] is True
    assert result["older_pagination_supported"] is True
    assert [row["t"] for row in result["candles"]] == [BOUND - 3600]
    query = parse_qs(urlparse(urls[0]).query)
    assert query["granularity"] == ["3600"]
    assert int(query["end"][0]) == BOUND - 1
    assert int(query["end"][0]) - int(query["start"][0]) == 299 * 3600


def test_coinbase_older_page_skips_empty_trading_window():
    urls = []

    def fetch(url, timeout=5.0):
        urls.append(url)
        if len(urls) == 1:
            return []
        second_end = int(parse_qs(urlparse(url).query)["end"][0])
        return [[second_end - 3600, 9, 11, 10, 10, 1]]

    rows = CoinbaseAdapter(http_get=fetch).get_candles_before(
        "BTC/USD", timeframe="1h", limit=200, before=BOUND)
    assert len(urls) == 2
    first = parse_qs(urlparse(urls[0]).query)
    second = parse_qs(urlparse(urls[1]).query)
    assert int(second["end"][0]) == int(first["start"][0]) - 1
    assert len(rows) == 1 and rows[0]["t"] < int(first["start"][0])


def test_coinbase_older_page_bounds_sparse_search_and_reports_network_failure():
    urls = []
    empty = CoinbaseAdapter(http_get=lambda url, timeout=5.0: (urls.append(url), [])[1])
    assert empty.get_candles_before("BTC/USD", timeframe="1m", before=BOUND) == []
    assert len(urls) == 8

    calls = []

    def fail_after_gap(url, timeout=5.0):
        calls.append(url)
        if len(calls) == 1:
            return []
        raise TimeoutError("simulated provider outage")

    failed = CoinbaseAdapter(http_get=fail_after_gap)
    assert failed.get_candles_before("BTC/USD", timeframe="1m", before=BOUND) is None
    assert len(calls) == 2


def test_coinbase_forward_cursor_has_both_bounds_and_native_timeframes():
    urls = []

    def fetch(url, timeout=5.0):
        urls.append(url)
        return [[BOUND - 900, 9, 11, 10, 10, 1],
                [BOUND, 9, 11, 10, 10, 1]]

    rows = CoinbaseAdapter(http_get=fetch).get_candles(
        "BTC/USD", timeframe="15m", limit=500, since=BOUND)
    assert [row["t"] for row in rows] == [BOUND]
    query = parse_qs(urlparse(urls[0]).query)
    assert int(query["end"][0]) - int(query["start"][0]) == 299 * 900
    assert query["granularity"] == ["900"]
    assert "30m" not in CoinbaseAdapter.timeframes
    assert "4h" not in CoinbaseAdapter.timeframes
    unsupported = service.candles("BTC/USD", provider="coinbase", timeframe="4h")
    assert unsupported["available"] is False
    assert "no nativo" in unsupported["reason"]


def test_coinbase_forward_page_keeps_first_matching_bucket_if_api_overlaps():
    adapter = CoinbaseAdapter(http_get=lambda url, timeout=5.0: [
        [BOUND - 900, 9, 11, 10, 10, 1],
        [BOUND, 9, 11, 10, 10, 1],
        [BOUND + 900, 9, 11, 10, 10, 1],
    ])
    rows = adapter.get_candles("BTC/USD", timeframe="15m", limit=1,
                               since=BOUND)
    assert [row["t"] for row in rows] == [BOUND]


def test_service_filters_overlap_and_reports_source_capability():
    urls = []

    def fetch(url, timeout=5.0):
        urls.append(url)
        return [
            [(BOUND - 3600) * 1000, "10", "11", "9", "10", "1"],
            [BOUND * 1000, "10", "11", "9", "10", "1"],
        ]

    result = service.candles("BTC/USDT", provider="binance", timeframe="1h",
                             limit=200, before=BOUND, http_get=fetch)
    assert result["available"] is True
    assert result["older_pagination_supported"] is True
    assert [row["t"] for row in result["candles"]] == [BOUND - 3600]
    assert f"endTime={BOUND * 1000 - 1}" in urls[0]


def test_unsupported_source_and_empty_older_history_are_explicit():
    unsupported = service.candles("BTC/EUR", provider="kraken",
                                   timeframe="1h", before=BOUND)
    assert unsupported["available"] is False
    assert "no ofrece paginación" in unsupported["reason"]
    empty = service.candles("BTC/USDT", provider="mexc", timeframe="1h",
                            before=BOUND, http_get=lambda url, timeout=5.0: [])
    assert empty["available"] is True
    assert empty["candles"] == []
    assert empty["history_exhausted"] is True
    failed = service.candles("BTC/USDT", provider="mexc", timeframe="1h",
                             before=BOUND, http_get=lambda url, timeout=5.0: None)
    assert failed["available"] is False
    assert failed.get("history_exhausted") is not True
    with pytest.raises(ValueError, match="exclusive"):
        service.candles("BTC/USDT", provider="mexc", before=BOUND,
                        since=BOUND - 3600)


def test_endpoint_rejects_conflicting_history_bounds(client):
    response = client.get(
        "/market/candles?symbol=BTC/USDT&provider=mexc&timeframe=1h"
        f"&since={BOUND - 3600}&before={BOUND}"
    )
    assert response.status_code == 400


def test_page_reports_internal_and_boundary_gaps_without_filling_them(monkeypatch):
    class Provider:
        timeframes = ("1h",)

        def get_candles_before(self, symbol, timeframe, limit, *, before):
            return [
                {"t": 3600, "o": "1", "h": "2", "l": "1", "c": "2", "v": "1"},
                {"t": 10800, "o": "2", "h": "3", "l": "2", "c": "3", "v": "1"},
            ]

    monkeypatch.setattr(service, "resolve_provider",
                        lambda provider, symbol, http_get: (Provider(), "fake"))
    result = service.candles("BTC/USDT", provider="fake", timeframe="1h",
                             limit=10, before=18000, http_get=lambda *args: None)
    assert result["available"] is True
    assert [row["t"] for row in result["candles"]] == [3600, 10800]
    assert result["gap_count"] == 1
    assert result["first_gap"] == {"from": 7200, "to": 10800}
    assert result["boundary_gap_seconds"] == 3600


def test_duplicate_candle_conflict_blocks_page(monkeypatch):
    class Provider:
        timeframes = ("1h",)

        def get_candles(self, symbol, timeframe, limit, since):
            base = {"t": 3600, "o": "1", "h": "2", "l": "1", "c": "2", "v": "1"}
            return [base, {**base, "c": "1.5"}]

    monkeypatch.setattr(service, "resolve_provider",
                        lambda provider, symbol, http_get: (Provider(), "fake"))
    result = service.candles("BTC/USDT", provider="fake", timeframe="1h",
                             http_get=lambda *args: None)
    assert result["available"] is False
    assert result["candles"] == []
    assert "incompatibles" in result["reason"]


@pytest.mark.parametrize("bad", [
    {"t": 7200, "o": "NaN", "h": "2", "l": "1", "c": "2", "v": "1"},
    {"t": 7200, "o": "1", "h": "0.5", "l": "1", "c": "2", "v": "1"},
    {"t": 7200, "o": "1", "h": "2", "l": "1", "c": "2", "v": "-1"},
    {"t": 7200, "o": "1", "h": "2", "l": "1", "v": "1"},
])
def test_invalid_candle_blocks_whole_page_instead_of_hiding_gap(monkeypatch, bad):
    class Provider:
        timeframes = ("1h",)

        def get_candles(self, symbol, timeframe, limit, since):
            return [
                {"t": 3600, "o": "1", "h": "2", "l": "1", "c": "2", "v": "1"},
                bad,
                {"t": 10800, "o": "2", "h": "3", "l": "2", "c": "3", "v": "1"},
            ]

    monkeypatch.setattr(service, "resolve_provider",
                        lambda provider, symbol, http_get: (Provider(), "fake"))
    result = service.candles("BTC/USDT", provider="fake", timeframe="1h",
                             http_get=lambda *args: None)
    assert result["available"] is False
    assert result["candles"] == []
    assert "inválida" in result["reason"]


def test_equivalent_duplicate_decimals_do_not_create_false_conflict(monkeypatch):
    class Provider:
        timeframes = ("1h",)

        def get_candles(self, symbol, timeframe, limit, since):
            base = {"t": 3600, "o": "1", "h": "2", "l": "1", "c": "2", "v": "1"}
            return [base, {**base, "c": "2.0"}]

    monkeypatch.setattr(service, "resolve_provider",
                        lambda provider, symbol, http_get: (Provider(), "fake"))
    result = service.candles("BTC/USDT", provider="fake", timeframe="1h",
                             http_get=lambda *args: None)
    assert result["available"] is True
    assert result["duplicate_count"] == 1
    assert len(result["candles"]) == 1
