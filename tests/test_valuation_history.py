from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from threading import Event
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio import valuation_history as history
from app.portfolio.api import create_real_router
from app.portfolio.domain import AccountingContext, EventInput, PortfolioEvent


NOW = datetime(2026, 11, 7, 18, tzinfo=timezone.utc)
MIDNIGHT = NOW.replace(hour=0)


def make_event(kind, when, currency="EUR", **values):
    identifier = str(uuid4())
    return PortfolioEvent(identifier, "a", identifier, NOW, EventInput(kind, currency, when, **values))


def series(symbol, provider, candles):
    return {"available": True, "symbol": symbol, "resolved_provider": provider, "timeframe": "1d", "candles": candles}


def candle(when, price, closed=True):
    return {"t": int(when.timestamp()), "c": price, "closed": closed}


def test_history_keeps_boundaries_flows_fees_and_missing_prices_separate(monkeypatch):
    first = MIDNIGHT - timedelta(days=7)
    next_day = first + timedelta(days=1)
    later = first + timedelta(days=2)
    final = first + timedelta(days=4)
    events = [
        make_event("CONTRIBUTION", first - timedelta(microseconds=1), amount=D("1000")),
        make_event("BUY", first, asset="BTC", quantity=D("1"), unit_price=D("100"), fee=D("0.01")),
        make_event("CONTRIBUTION", next_day, amount=D("200")),
        make_event("WITHDRAWAL", later, amount=D("50"), fee=D("0.02")),
        make_event("CASH_TRANSFER_IN", later, amount=D("2")),
        make_event("SELL", final, asset="BTC", quantity=D("1"), unit_price=D("140"), fee=D("0.03")),
        make_event("CONTRIBUTION", MIDNIGHT, amount=D("90")),
        make_event("BUY", first, "USDT", asset="ETH", quantity=D("2"), unit_price=D("10")),
    ]
    calls = []

    def quotes(symbol, **kwargs):
        calls.append((symbol, kwargs))
        return series(symbol, "kraken", [
            candle(first, "120.0000000000000000001"),
            candle(next_day, "1000", closed=False),
            candle(later, "130"),
            candle(later + timedelta(hours=12), "1"),
            candle(MIDNIGHT, "99999"),
        ])

    monkeypatch.setattr(history, "candles", quotes)
    report = history.value_history("a", list(reversed(events)), currency="EUR", provider="kraken", days=7, now=NOW)
    assert calls == [("BTC/EUR", {"provider": "kraken", "timeframe": "1d", "limit": 8, "since": int(first.timestamp())})]
    rows = report["rows"]
    assert report["end_t"] == int(MIDNIGHT.timestamp()) and len(rows) == 7
    assert rows[0]["equity"] == D("1019.9900000000000000001")
    assert rows[0]["total_pnl"] == D("19.9900000000000000001")
    assert rows[0]["contributions"] == 0
    assert rows[1]["equity"] is None and rows[1]["missing_assets"] == ["BTC"]
    assert rows[1]["contributions"] == 200 and rows[1]["withdrawals"] == 0
    assert rows[2]["equity"] == D("1181.97")
    assert rows[2]["withdrawals"] == 50 and rows[2]["total_pnl"] == D("29.97")
    assert rows[3]["equity"] is None and rows[3]["realized_pnl"] == D("-0.02")
    assert rows[4]["equity"] == rows[-1]["equity"] == D("1191.94")
    assert rows[-1]["total_pnl"] == D("39.94")


def test_history_uses_accepted_effective_dates_and_leaves_unrecorded_days_absent(monkeypatch):
    yesterday = MIDNIGHT - timedelta(days=1)
    original = make_event("CONTRIBUTION", yesterday - timedelta(seconds=1), amount=D("100"))
    effective = replace(original.input, occurred_at=yesterday)
    accepted = replace(original, accounting=AccountingContext(original, effective, 0, ("accepted",), True, None, False))
    monkeypatch.setattr(history, "candles", lambda *_a, **_k: pytest.fail("Cash does not require public pairs"))
    [row] = history.value_history("a", [accepted], currency="EUR", provider="mexc", days=365, now=NOW)["rows"]
    assert row["date_t"] == int(yesterday.timestamp())
    assert row["equity"] == 100 and row["contributions"] == 100 and row["total_pnl"] == 0
    assert history.value_history("a", [accepted], currency="USDT", provider="mexc", days=30, now=NOW)["rows"] == []


@pytest.mark.parametrize("problem", ["provider", "pair", "timeframe", "conflict", "misaligned", "invalid"])
def test_history_rejects_unusable_public_prices_without_hiding_book_totals(monkeypatch, problem):
    day = MIDNIGHT - timedelta(days=1)
    payload = series("BTC/EUR", "kraken", [candle(day, "125")])
    if problem == "provider":
        payload["resolved_provider"] = "bitstamp"
    elif problem == "pair":
        payload["symbol"] = "BTC/USD"
    elif problem == "timeframe":
        payload["timeframe"] = "1h"
    elif problem == "conflict":
        payload["candles"].append(candle(day, "150"))
    elif problem == "misaligned":
        payload["candles"] = [candle(day + timedelta(seconds=1), "125")]
    elif problem == "invalid":
        payload["candles"] = [candle(day, "NaN")]
    monkeypatch.setattr(history, "candles", lambda *_a, **_k: payload)
    events = [make_event("BUY", day, asset="BTC", quantity=D("1"), unit_price=D("100"))]
    [row] = history.value_history("a", events, currency="EUR", provider="kraken", days=7, now=NOW)["rows"]
    assert row["negative_cash"] and row["cash"] == -100 and row["realized_pnl"] == 0
    assert row["missing_assets"] == ["BTC"]
    assert row["equity"] is row["unrealized_pnl"] is row["total_pnl"] is None


@pytest.mark.parametrize("during", ["lock", "write"])
def test_http_history_checks_auth_and_keeps_one_book_snapshot(tmp_path, monkeypatch, during):
    entered, release = Event(), Event()

    def load(symbol, *, provider, timeframe, limit, since):
        entered.set()
        assert release.wait(timeout=10)
        return series(symbol, provider, [{"t": since + history.DAY * 6, "c": "123.4567890123456789", "closed": True}])

    monkeypatch.setattr(history, "candles", load)
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "history.db"))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        token = client.post("/real/vault/create", json={"passphrase": "owned history password"}).json()["token"]
        headers = {"X-Orbis-Vault-Token": token, "X-Orbis-Passive": "1"}
        account = client.post("/real/accounts", headers=headers, json={"name": "Owned", "base_currency": "EUR"}).json()["id"]
        path = f"/real/accounts/{account}"
        url = path + "/valuation-history"
        params = {"currency": "EUR", "days": 7, "provider": "kraken"}
        assert client.get(url, params=params).status_code == 401
        assert client.get(url, params=params, headers={**headers, "Origin": "https://outside.invalid"}).status_code == 403
        assert client.get(url, params={**params, "days": history.MAX_VALUATION_DAYS + 1}, headers=headers).status_code == 422
        assert client.get(url, params={**params, "provider": "auto"}, headers=headers).status_code == 422
        when = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=12)

        def add(kind, offset=0, **values):
            return client.post(path + "/events", headers=headers, json={
                "request_id": str(uuid4()), "kind": kind, "quote_currency": "EUR",
                "occurred_at": (when + timedelta(seconds=offset)).isoformat(), **values,
            })

        assert add("CONTRIBUTION", amount="1000").status_code == 200
        assert add("BUY", 1, asset="BTC", quantity="1", unit_price="100").status_code == 200
        pending = executor.submit(client.get, url, headers=headers, params=params)
        try:
            assert entered.wait(timeout=10)
            if during == "lock":
                assert client.post("/real/vault/lock", headers=headers).status_code == 200
            else:
                assert add("CONTRIBUTION", 2, amount="500").status_code == 200
        finally:
            release.set()
        response = pending.result(timeout=10)
        if during == "lock":
            assert response.status_code == 401 and set(response.json()) == {"detail"}
        else:
            assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
            [row] = response.json()["rows"]
            assert row["equity"] == "1023.4567890123456789" and row["contributions"] == "1000"
            updated = client.get(url, headers=headers, params=params).json()["rows"][0]
            assert updated["equity"] == "1523.4567890123456789" and updated["contributions"] == "1500"
            assert updated["total_pnl"] == row["total_pnl"] == "23.4567890123456789"


@pytest.mark.parametrize("provider,cap", [("bitstamp", 1000), ("binance", 1000), ("mexc", 500)])
def test_multi_year_history_uses_disjoint_native_pages_and_keeps_a_missing_page(monkeypatch, provider, cap):
    days = 1826
    first = MIDNIGHT - timedelta(days=days)
    end = int(MIDNIGHT.timestamp())
    first_t = int(first.timestamp())
    events = [
        make_event("CONTRIBUTION", first, amount=D("1000")),
        make_event("BUY", first + timedelta(seconds=1), asset="BTC", quantity=D("1"), unit_price=D("100")),
        make_event("BUY", MIDNIGHT - timedelta(days=2), asset="ETH", quantity=D("1"), unit_price=D("20")),
    ]
    calls = []

    def fetch(symbol, *, provider, timeframe, limit, before):
        calls.append((symbol, provider, timeframe, limit, before))
        if symbol == "BTC/EUR" and before == end - cap * history.DAY:
            return {"available": False}
        price = "125" if symbol == "BTC/EUR" else "25"
        return series(symbol, provider, [
            {"t": stamp, "c": price, "closed": True}
            for stamp in range(before - limit * history.DAY, before, history.DAY)
        ] + [{"t": before, "c": "99999", "closed": False}])

    monkeypatch.setattr(history, "candles", fetch)
    result = history.value_history("a", events, currency="EUR", provider=provider, days=days, now=NOW)
    expected = [(min(cap, (right - first_t) // history.DAY), right)
                for right in range(end, first_t, -cap * history.DAY)]
    assert [(limit, before) for symbol, _, _, limit, before in calls if symbol == "BTC/EUR"] == expected
    assert [(limit, before) for symbol, _, _, limit, before in calls if symbol == "ETH/EUR"] == [(2, end)]
    assert all(venue == provider and timeframe == "1d" for _, venue, timeframe, _, _ in calls)
    rows = result["rows"]
    assert len(rows) == days and rows[0]["date_t"] == first_t
    assert sum(row["missing_assets"] == ["BTC"] for row in rows) == expected[1][0]
    assert rows[-1]["equity"] == 1030 and rows[-1]["total_pnl"] == 30
    assert rows[-1]["cash"] == 880 and rows[-1]["contributions"] == 0


def test_kraken_long_history_keeps_missing_days_without_switching_providers(monkeypatch):
    first = MIDNIGHT - timedelta(days=1826)
    events = [
        make_event("CONTRIBUTION", first, amount=D("1000")),
        make_event("BUY", first + timedelta(seconds=1), asset="BTC", quantity=D("1"), unit_price=D("100")),
    ]
    calls = []

    def recent(symbol, **kwargs):
        calls.append(kwargs)
        return series(symbol, "kraken", [candle(MIDNIGHT - timedelta(days=1), "125")])

    monkeypatch.setattr(history, "candles", recent)
    rows = history.value_history("a", events, currency="EUR", provider="kraken", days=1826, now=NOW)["rows"]
    assert len(calls) == 1 and calls[0]["provider"] == "kraken" and "before" not in calls[0]
    assert len(rows) == 1826 and rows[0]["equity"] is None and rows[0]["cash"] == 900
    assert rows[-1]["equity"] == 1025
