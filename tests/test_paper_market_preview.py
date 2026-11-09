"""La referencia previa debe usar el mismo validador que la orden PAPER."""

from datetime import datetime, timedelta, timezone


def snapshot(*, received_at=None, market_time_at=None, provider="kraken", **overrides):
    now = datetime.now(timezone.utc)
    payload = {
        "available": True, "symbol": "BTC/EUR", "resolved_provider": provider,
        "source": provider, "price": "100", "ask": "101", "bid": "99",
        "received_at": (received_at or now).isoformat(),
        "market_time_at": (market_time_at or now).isoformat(),
        "market_time_status": "fresh",
    }
    payload.update(overrides)
    return payload


def test_preview_reports_buy_ask_and_sell_bid_without_writing(client, monkeypatch):
    calls = []

    def market(symbol, provider=None):
        calls.append((symbol, provider))
        return snapshot()

    monkeypatch.setattr("app.main.market_snapshot", market)
    response = client.get("/paper/market/preview?asset=BTC&quote_currency=EUR&provider=kraken")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["buy_price"] == "101"
    assert body["sell_price"] == "99"
    assert body["buy_price_basis"] == "ask"
    assert body["sell_price_basis"] == "bid"
    assert body["provider"] == "kraken"
    assert body["stale"] is False
    assert calls == [("BTC/EUR", "kraken")]
    assert client.get("/operations").json() == []


def test_preview_rejects_stale_and_wrong_source(client, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(minutes=3)
    monkeypatch.setattr("app.main.market_snapshot", lambda *_args, **_kwargs: snapshot(
        received_at=old, market_time_at=old,
    ))
    stale = client.get("/paper/market/preview?asset=BTC&quote_currency=EUR&provider=kraken")
    assert stale.status_code == 200
    assert stale.json()["available"] is False
    assert stale.json()["buy_price"] is None

    monkeypatch.setattr("app.main.market_snapshot", lambda *_args, **_kwargs: snapshot(provider="mexc"))
    wrong = client.get("/paper/market/preview?asset=BTC&quote_currency=EUR&provider=kraken")
    assert wrong.status_code == 200
    assert wrong.json()["available"] is False


def test_preview_marks_last_price_approximate(client, monkeypatch):
    monkeypatch.setattr("app.main.market_snapshot", lambda *_args, **_kwargs: snapshot(
        ask=None, bid=None, market_time_at=None, market_time_status="unknown",
    ))
    response = client.get("/paper/market/preview?asset=BTC&quote_currency=EUR&provider=kraken")
    assert response.status_code == 200
    body = response.json()
    assert body["buy_price"] == body["sell_price"] == "100"
    assert body["buy_approximate"] is True
    assert body["sell_approximate"] is True
    assert body["buy_price_basis"] == body["sell_price_basis"] == "last"


def test_preview_keeps_sell_available_when_ask_is_invalid(client, monkeypatch):
    monkeypatch.setattr("app.main.market_snapshot", lambda *_args, **_kwargs: snapshot(ask="0"))
    response = client.get("/paper/market/preview?asset=BTC&quote_currency=EUR&provider=kraken")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["buy_price"] is None
    assert body["sell_price"] == "99"
    assert body["sell_price_basis"] == "bid"
    assert body["price"] == "99"


def test_preview_keeps_buy_available_when_bid_is_invalid(client, monkeypatch):
    monkeypatch.setattr("app.main.market_snapshot", lambda *_args, **_kwargs: snapshot(bid="0"))
    response = client.get("/paper/market/preview?asset=BTC&quote_currency=EUR&provider=kraken")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["buy_price"] == "101"
    assert body["sell_price"] is None
    assert body["buy_price_basis"] == "ask"


def test_preview_reports_provider_failure_without_server_error(client, monkeypatch):
    monkeypatch.setattr("app.main.market_snapshot", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("upstream")))
    response = client.get("/paper/market/preview?asset=BTC&quote_currency=EUR&provider=kraken")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["buy_price"] is None and body["sell_price"] is None
