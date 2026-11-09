"""Precios públicos (Kraken primario, CoinGecko de recurso): solo valoración.

Todo con dobles de red (la suite debe ser verde sin internet): éxito, fallo
de Kraken con recurso a CoinGecko, fallo total con fallback a último
trade/coste medio sin bloquear, TTL de caché y OHLC por timeframe. El sondeo
real contra Kraken/CoinGecko se hace a mano (ver informe) por su rate-limit.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.pricing import providers as pub
from app.pricing.providers import (
    CoinGeckoPublicPriceProvider,
    KrakenPublicPriceProvider,
    fetch_coingecko_price,
    fetch_kraken_ohlc,
    fetch_kraken_ticker,
    get_public_history,
    get_public_ticker_price,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    pub._ticker_cache.clear()
    pub._ohlc_cache.clear()
    yield
    pub._ticker_cache.clear()
    pub._ohlc_cache.clear()


def _kraken_ticker_ok(url, timeout=5.0):
    assert "api.kraken.com" in url
    return {"error": [], "result": {"XXBTZEUR": {"c": ["77317.50000", "0.01"]}}}


def _kraken_ohlc_ok(url, timeout=5.0):
    assert "api.kraken.com" in url
    assert "interval=" in url
    return {"error": [], "result": {"XXBTZEUR": [
        [1700000000, "77000", "77100", "76900", "77050", "1.0", 10],
        [1700000060, "77050", "77200", "77000", "77150", "2.0", 12],
    ], "last": 1700000060}}


def _coingecko_ok(url, timeout=5.0):
    assert "coingecko" in url
    return {"bitcoin": {"eur": 77334}}


def _always_fails(url, timeout=5.0):
    raise ConnectionError("red caída")


def test_kraken_ticker_parses_last_close():
    price, as_of = fetch_kraken_ticker("BTC", "EUR", http_get=_kraken_ticker_ok)
    assert price == Decimal("77317.50000")
    assert as_of


def test_kraken_error_payload_raises():
    def bad(url, timeout=5.0):
        return {"error": ["EQuery:Unknown asset pair"], "result": {}}
    with pytest.raises(ValueError):
        fetch_kraken_ticker("XXX", "EUR", http_get=bad)


def test_coingecko_parses_simple_price():
    price, _ = fetch_coingecko_price("BTC", "EUR", http_get=_coingecko_ok)
    assert price == Decimal("77334")


def test_coingecko_without_mapping_raises():
    with pytest.raises(ValueError):
        fetch_coingecko_price("XXX", "EUR", http_get=_coingecko_ok)


def test_public_ticker_falls_back_to_coingecko_when_kraken_fails():
    def router(url, timeout=5.0):
        if "kraken" in url:
            raise ConnectionError("kraken caído")
        return _coingecko_ok(url, timeout)
    price, source, _, stale = get_public_ticker_price("BTC", "EUR", http_get=router)
    assert price == Decimal("77334")
    assert source == "coingecko"
    assert stale is True


def test_public_ticker_total_failure_returns_none_without_raising():
    price, source, as_of, stale = get_public_ticker_price(
        "BTC", "EUR", http_get=_always_fails
    )
    assert price is None and as_of is None and stale is True
    assert KrakenPublicPriceProvider(http_get=_always_fails).get_price("BTC", "EUR") is None
    assert CoinGeckoPublicPriceProvider(http_get=_always_fails).get_price("BTC", "EUR") is None


def test_ticker_cache_avoids_repeat_calls_within_ttl():
    calls = []

    def counting(url, timeout=5.0):
        calls.append(url)
        return _kraken_ticker_ok(url, timeout)

    # Llamadas con red simulada a través del getter por defecto
    import app.pricing.providers as P
    real = P._http_get_json
    P._http_get_json = counting
    try:
        first, _, _, _ = get_public_ticker_price("BTC", "EUR")
        second, source, _, _ = get_public_ticker_price("BTC", "EUR")
    finally:
        P._http_get_json = real
    assert first == Decimal("77317.50000")
    assert source == "kraken-cache"
    assert len(calls) == 1


def test_ohlc_history_returns_closes_per_timeframe():
    for timeframe, interval in (("1h", 1), ("24h", 60), ("7d", 1440)):
        seen = {}

        def scoped(url, timeout=5.0, _i=interval):
            assert f"interval={_i}" in url
            seen["ok"] = True
            return _kraken_ohlc_ok(url, timeout)

        history = get_public_history("BTC", "EUR", timeframe, http_get=scoped,
                                     now_epoch=1700000060)
        assert seen.get("ok")
        assert history["interval"] == interval
        assert [p["close"] for p in history["points"]] == ["77050", "77150"]
        assert history["stale"] is False


@pytest.mark.parametrize("timeframe,interval,seconds,window", [
    ("1h", 1, 60, 3600),
    ("24h", 60, 3600, 86400),
    ("7d", 1440, 86400, 7 * 86400),
])
def test_history_covers_only_requested_window(timeframe, interval, seconds, window):
    """Kraken devuelve hasta 720 velas aunque la UI pida una ventana corta."""
    end = 1_800_000_000
    rows = [[end - i * seconds, "100", "102", "99", "101", "1", "10"]
            for i in range(2 * window // seconds, -1, -1)]

    def source(url, timeout=5.0):
        assert f"interval={interval}" in url
        return {"error": [], "result": {"XXBTZEUR": rows, "last": end}}

    result = get_public_history("BTC", "EUR", timeframe, http_get=source,
                                now_epoch=end + seconds)
    assert result["points"][-1]["t"] == end
    assert result["points"][0]["t"] >= end - window
    assert len(result["points"]) < len(rows)


def test_paper_portfolio_seven_day_history_does_not_include_years(client, monkeypatch):
    """La misma ruta que usa Cartera PAPER debe enviar sólo siete días."""
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "kraken")
    end = 1_800_000_000
    rows = [[end - day * 86400, "100", "102", "99", "101", "1", "10"]
            for day in range(30, -1, -1)]

    def source(url, timeout=5.0):
        assert "interval=1440" in url
        return {"error": [], "result": {"XXBTZEUR": rows, "last": end}}

    monkeypatch.setattr(pub, "_http_get_json", source)
    response = client.get("/market/history?asset=BTC&quote_currency=EUR&timeframe=7d")
    assert response.status_code == 200
    points = response.json()["points"]
    assert points[-1]["t"] == end
    assert points[0]["t"] >= end - 7 * 86400
    assert len(points) < len(rows)


@pytest.mark.parametrize("timeframe,interval_seconds", [
    ("1h", 60), ("24h", 3600), ("7d", 86400),
])
def test_historical_response_old_by_two_candles_is_marked_stale(timeframe, interval_seconds):
    end = 1_700_000_000

    def source(url, timeout=5.0):
        return {"error": [], "result": {"XXBTZEUR": [
            [end - interval_seconds, "100", "102", "99", "101", "1", "10"],
            [end, "101", "103", "100", "102", "1", "11"],
        ], "last": end}}

    fresh = get_public_history("BTC", "EUR", timeframe, http_get=source,
                               now_epoch=end + 2 * interval_seconds)
    assert fresh["stale"] is False
    assert fresh["note"] is None
    old = get_public_history("BTC", "EUR", timeframe, http_get=source,
                             now_epoch=end + 2 * interval_seconds + 1)
    assert old["stale"] is True
    assert "antigua" in old["note"]
    assert old["last_candle_at"] is not None
    assert old["age_seconds"] == 2 * interval_seconds + 1
    assert old["points"]  # Solo se marca; no se inventa ni borra el histórico.


def test_empty_or_future_dated_history_is_not_fresh():
    end = 1_700_000_000

    def empty(url, timeout=5.0):
        return {"error": [], "result": {"XXBTZEUR": [], "last": end}}

    no_points = get_public_history("BTC", "EUR", "7d", http_get=empty, now_epoch=end)
    assert no_points["stale"] is True and no_points["points"] == []
    assert no_points["last_candle_at"] is None

    def future(url, timeout=5.0):
        return {"error": [], "result": {"XXBTZEUR": [
            [end + 3600, "100", "102", "99", "101", "1", "10"]],
            "last": end + 3600}}

    impossible = get_public_history("BTC", "EUR", "7d", http_get=future,
                                    now_epoch=end)
    assert impossible["stale"] is True
    assert "futura" in impossible["note"]


def test_recently_fetched_cache_does_not_make_old_candles_fresh(monkeypatch):
    monkeypatch.setattr(pub, "_http_get_json", _kraken_ohlc_ok)
    first = get_public_history("BTC", "EUR", "24h")
    second = get_public_history("BTC", "EUR", "24h")
    assert first["source"] == "kraken" and second["source"] == "kraken-cache"
    assert first["stale"] is True and second["stale"] is True
    assert second["last_candle_at"] == first["last_candle_at"]


def test_impossible_timestamp_does_not_crash_history():
    def invalid(url, timeout=5.0):
        return {"error": [], "result": {"XXBTZEUR": [
            [10**30, "100", "102", "99", "101", "1", "10"]], "last": 10**30}}

    result = get_public_history("BTC", "EUR", "7d", http_get=invalid,
                                now_epoch=1_700_000_000)
    assert result["stale"] is True
    assert result["last_candle_at"] is None
    assert "fecha inválida" in result["note"]


def test_live_timeframe_has_no_fake_history():
    history = get_public_history("BTC", "EUR", "live", http_get=_kraken_ticker_ok)
    assert history["points"] == []
    assert "sin serie histórica" in (history["note"] or "")


def test_ohlc_failure_returns_honest_note_without_inventing():
    history = get_public_history("BTC", "EUR", "24h", http_get=_always_fails)
    assert history["points"] == []
    assert history["stale"] is True
    assert "No se inventan datos" in (history["note"] or "")


def test_unknown_timeframe_is_rejected():
    with pytest.raises(ValueError):
        get_public_history("BTC", "EUR", "1m", http_get=_kraken_ohlc_ok)


def test_market_price_endpoint_serves_public_quote(client, monkeypatch):
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "kraken")
    monkeypatch.setattr(pub, "_http_get_json", _kraken_ticker_ok)
    response = client.get("/market/price?asset=BTC&quote_currency=EUR")
    assert response.status_code == 200
    payload = response.json()
    assert payload["price"] == "77317.50000"
    assert payload["source"] in ("kraken", "kraken-cache")
    assert payload["stale"] is False
    assert client.get("/market/price?asset=BTC&quote_currency=eur-mal").status_code == 400


def test_market_history_endpoint_serves_ohlc_or_honest_note(client, monkeypatch):
    import time

    latest = int(time.time() // 3600) * 3600

    def recent(url, timeout=5.0):
        assert "api.kraken.com" in url
        return {"error": [], "result": {"XXBTZEUR": [
            [latest - 3600, "77000", "77100", "76900", "77050", "1.0", 10],
            [latest, "77050", "77200", "77000", "77150", "2.0", 12],
        ], "last": latest}}

    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "kraken")
    monkeypatch.setattr(pub, "_http_get_json", recent)
    response = client.get("/market/history?asset=BTC&quote_currency=EUR&timeframe=24h")
    assert response.status_code == 200
    assert len(response.json()["points"]) == 2

    monkeypatch.setattr(pub, "_http_get_json", _always_fails)
    fallback = client.get("/market/history?asset=BTC&quote_currency=EUR&timeframe=24h")
    assert fallback.status_code == 200
    # Caché aún fresca (5min): se sirve sin marcar stale.
    assert fallback.json()["stale"] is False
    assert fallback.json()["source"] == "kraken-cache"

    # Caché caducada + red caída: última serie marcada stale con aviso.
    import time as _time
    key = ("BTC", "EUR", 60)
    assert key in pub._ohlc_cache
    _, pts = pub._ohlc_cache[key]
    pub._ohlc_cache[key] = (_time.monotonic() - 400, pts)
    stale = client.get("/market/history?asset=BTC&quote_currency=EUR&timeframe=24h")
    assert stale.status_code == 200
    assert stale.json()["stale"] is True
    assert stale.json()["source"] == "stale-cache"
    assert len(stale.json()["points"]) == 2

    pub._ohlc_cache.clear()
    empty = client.get("/market/history?asset=BTC&quote_currency=EUR&timeframe=24h")
    assert empty.status_code == 200
    assert empty.json()["points"] == []
    assert "No se inventan datos" in (empty.json()["note"] or "")


def test_kraken_outage_never_blocks_portfolio_valuation(client, create_scenario, monkeypatch):
    create_scenario()
    client.post("/paper/buy", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "order_amount": "100", "price": "100000", "estimated_fee": "0",
    })
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "kraken")
    monkeypatch.setattr(pub, "_http_get_json", _always_fails)
    portfolio = client.get("/paper/portfolio?quote_currency=EUR")
    assert portfolio.status_code == 200
    [position] = portfolio.json()["positions"]
    # Sin red: último precio negociado (100000), sin excepción ni bloqueo.
    assert Decimal(str(position["current_price"])) == Decimal("100000")


def test_market_with_manual_provider_makes_no_network_calls(client, monkeypatch):
    """Con ORBIS_PRICE_PROVIDER=manual /market no toca la red (A-01)."""
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "manual")

    def _forbidden(url, timeout=5.0):
        raise AssertionError(f"red tocada con provider manual: {url}")

    monkeypatch.setattr(pub, "_http_get_json", _forbidden)

    published = client.post("/paper/prices", json={
        "asset": "BTC", "quote_currency": "EUR", "price": "120000",
    })
    assert published.status_code == 201

    price = client.get("/market/price?asset=BTC&quote_currency=EUR")
    assert price.status_code == 200
    payload = price.json()
    assert Decimal(str(payload["price"])) == Decimal("120000")
    assert payload["source"] == "manual"

    history = client.get("/market/history?asset=BTC&quote_currency=EUR&timeframe=24h")
    assert history.status_code == 200
    assert history.json()["points"] == []
    assert "No se inventan datos" in (history.json()["note"] or "")

    live = client.get("/market/history?asset=BTC&quote_currency=EUR&timeframe=live")
    assert live.status_code == 200
    assert live.json()["points"] == []


def test_market_with_static_provider_makes_no_network_calls(client, monkeypatch):
    """Con ORBIS_PRICE_PROVIDER=static /market no toca la red (A-01)."""
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "static")

    def _forbidden(url, timeout=5.0):
        raise AssertionError(f"red tocada con provider static: {url}")

    monkeypatch.setattr(pub, "_http_get_json", _forbidden)

    price = client.get("/market/price?asset=BTC&quote_currency=EUR")
    assert price.status_code == 200
    assert price.json()["price"] is None
    assert price.json()["stale"] is True

    history = client.get("/market/history?asset=BTC&quote_currency=EUR&timeframe=24h")
    assert history.status_code == 200
    assert history.json()["points"] == []


def test_market_with_coingecko_provider_uses_coingecko_first(client, monkeypatch):
    """Con ORBIS_PRICE_PROVIDER=coingecko Kraken deja de ser el primario."""
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "coingecko")
    # Si /market llamara a Kraken, este doble fallaría por la aserción de URL.
    monkeypatch.setattr(pub, "_http_get_json", _coingecko_ok)

    payload = client.get("/market/price?asset=BTC&quote_currency=EUR").json()
    assert payload["price"] == "77334"
    assert payload["source"] == "coingecko"
    assert payload["stale"] is False


def test_manual_fallback_reports_real_age_from_updated_at(
    client, create_scenario, db, monkeypatch
):
    """En fallback a manual la edad sale de `updated_at`, no de caché (C-06)."""
    from datetime import datetime, timedelta, timezone

    from app.database.models import PriceQuote

    create_scenario()
    published = client.post("/paper/prices", json={
        "asset": "BTC", "quote_currency": "EUR", "price": "120000",
    })
    assert published.status_code == 201

    # Envejecer la cotización 90s: la edad servida debe reflejarlo.
    row = db.query(PriceQuote).filter_by(asset="BTC", quote_currency="EUR").one()
    row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    db.commit()

    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "kraken")
    monkeypatch.setattr(pub, "_http_get_json", _always_fails)
    payload = client.get("/market/price?asset=BTC&quote_currency=EUR").json()

    assert payload["source"] == "manual"
    assert Decimal(str(payload["price"])) == Decimal("120000")
    assert payload["stale"] is True
    assert payload["as_of"] is not None
    assert payload["age_seconds"] is not None and payload["age_seconds"] >= 89
