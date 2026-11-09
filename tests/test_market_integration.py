"""Integración TERMINAL (separada de unit): endpoints readonly + WS + UI state."""

from fastapi.testclient import TestClient
import pytest

from app.marketdata import cache as market_cache


@pytest.fixture(autouse=True)
def isolated_market_cache():
    market_cache.clear()
    yield
    market_cache.clear()


def _kraken_ok(url, timeout=5.0):
    if "OHLC" in url:
        return {"error": [], "result": {"XXBTZEUR": [
            [1700000000, "77000", "77100", "76900", "77050", "1.0", 10],
        ], "last": 1700000000}}
    return {"error": [], "result": {"XXBTZEUR": {"c": ["77317.50000", "0.01"]}}}


def test_market_endpoints_readonly(client, monkeypatch):
    import app.marketdata.http as H

    monkeypatch.setattr(H, "http_get_json", _kraken_ok)

    providers = client.get("/market/providers")
    assert providers.status_code == 200
    assert len(providers.json()["providers"]) == 11
    assert "1d" in providers.json()["timeframes"]["coinbase"]
    assert "4h" not in providers.json()["timeframes"]["coinbase"]
    assert providers.json()["timeframes"]["bitstamp"] == ["1d"]

    symbols = client.get("/market/symbols?provider=kraken&search=BTC")
    assert symbols.status_code == 200
    assert "BTC/EUR" in symbols.json()["providers"]["kraken"]

    snap = client.get("/market/snapshot?symbol=BTC/EUR&provider=kraken")
    assert snap.status_code == 200
    assert isinstance(snap.json()["price"], str)
    assert snap.json()["resolved_provider"] == "kraken"

    missing = client.get("/market/snapshot?symbol=XXX/EUR&provider=binance")
    assert missing.json()["available"] is False

    candles = client.get("/market/candles?symbol=BTC/EUR&provider=kraken&timeframe=1h&limit=50")
    assert candles.status_code == 200
    assert candles.json()["available"] is True

    bad_tf = client.get("/market/candles?symbol=BTC/EUR&provider=kraken&timeframe=3m")
    assert bad_tf.status_code == 400

    alt = client.get("/market/pair-alternatives?base=BTC&provider=kraken")
    assert alt.status_code == 200
    assert "no disponible en kraken" in alt.json()["message"]

    # Legacy intacto.
    assert client.get("/market/price?asset=BTC&quote_currency=EUR").status_code == 200


def test_market_stream_websocket_and_sse(client, monkeypatch):
    import app.marketdata.http as H

    monkeypatch.setattr(H, "http_get_json", _kraken_ok)
    with client.websocket_connect("/market/stream?symbol=BTC/EUR&provider=kraken") as ws:
        ws.send_text("ping")
        import json

        payload = json.loads(ws.receive_text())
        assert payload["symbol"] == "BTC/EUR"
    sse = client.get("/market/stream-sse?symbol=BTC/EUR&provider=kraken&rounds=1")
    assert sse.status_code == 200
    assert "data:" in sse.text


def test_ui_state_watchlist_preferences_roundtrip(client):
    put = client.put("/ux/state", json={
        "market": "BTC/EUR", "provider": "kraken", "timeframe": "1h",
        "kind": "drawings", "payload": {"items": [{"id": "d1", "type": "h"}]},
    })
    assert put.status_code == 200
    got = client.get("/ux/state?market=btc/eur&provider=kraken&timeframe=1h&kind=drawings")
    assert got.json()["payload"] == {"items": [{"id": "d1", "type": "h"}]}
    # Normalizado: minúsculas resuelven al mismo mercado.
    assert got.json()["market"] == "BTC/EUR"

    created = client.post("/ux/watchlist", json={"market": "BTC/EUR", "provider": "kraken"})
    assert created.status_code == 201
    listed = client.get("/ux/watchlist")
    assert any(w["market"] == "BTC/EUR" for w in listed.json())
    assert client.delete("/ux/watchlist?market=BTC/EUR").json()["deleted"] >= 1

    saved = client.put("/ux/preferences", json={"key": "orbis.mode", "value": "PAPER"})
    assert saved.status_code == 200
    assert client.get("/ux/preferences?key=orbis.mode").json()["value"] == "PAPER"


def test_market_candles_monthly_end_to_end(client, monkeypatch):
    """R1: la puerta de ``mount.py`` deja pasar 1M (binance, mensual real en
    ms→s) y sigue devolviendo 400 honesto para lo que ningún exchange sirve
    (1y) o no existe (3m)."""
    import app.marketdata.http as H
    import app.marketdata.registry as reg

    # El registry memoiza instancias y cada adapter captura http_get_json al
    # construirse: sin vaciar el memo, este test reutilizaría la instancia de
    # binance creada por tests anteriores con otro mock. Se restaura solo.
    monkeypatch.setattr(reg, "_instances", {})

    def _binance_1m(url, timeout=5.0):
        assert "interval=1M" in url, url
        # 2025-08-01 00:00:00 UTC en ms, como lo devuelve binance.
        return [[1754006400000, "110000", "115000", "108000", "113000", "10"]]

    monkeypatch.setattr(H, "http_get_json", _binance_1m)

    ok = client.get("/market/candles?symbol=BTC/EUR&provider=binance&timeframe=1M&limit=5")
    assert ok.status_code == 200
    body = ok.json()
    assert body["available"] is True
    assert body["timeframe"] == "1M"
    assert body["resolved_provider"] == "binance"
    assert body["candles"] and body["candles"][0]["t"] == 1754006400

    for bad in ("1y", "3m"):
        rejected = client.get(
            f"/market/candles?symbol=BTC/EUR&provider=binance&timeframe={bad}&limit=5")
        assert rejected.status_code == 400
        assert bad in rejected.json()["detail"]
