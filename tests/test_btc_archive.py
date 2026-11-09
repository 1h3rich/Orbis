"""Archivo BTC 2012: datos públicos auténticos, paginación y límites."""

from app.marketdata.adapters.bitstamp import BitstampAdapter, DAY
from app.marketdata.archive import START, START_EUR, btc_daily_archive


def _row(t, close="5.00"):
    return {"timestamp": str(t), "open": "4.00", "high": "6.00", "low": "3.00", "close": close, "volume": "2.50"}


def test_bitstamp_adapter_daily_only_validates_and_sorts():
    def get(url):
        assert "step=86400" in url and "exclude_current_candle=true" in url
        return {"data": {"ohlc": [_row(START + DAY), _row(START), _row(START + DAY), _row(START + 2 * DAY, "NaN")]}}

    adapter = BitstampAdapter(http_get=get)
    rows = adapter.get_candles("BTC/USD", "1d", 4, START)
    assert [row["t"] for row in rows] == [START, START + DAY]
    assert all(row["closed"] for row in rows)
    assert adapter.get_candles("BTC/EUR", "1d") is not None
    assert adapter.get_candles("BTC/USD", "1h") is None


def test_bitstamp_eur_uses_its_own_public_market():
    urls = []

    def get(url):
        urls.append(url)
        return {"data": {"ohlc": [_row(START_EUR)]}}

    adapter = BitstampAdapter(http_get=get)
    assert adapter.get_candles("BTC/EUR", "1d", 1000, START_EUR)[0]["t"] == START_EUR
    assert "/ohlc/btceur/" in urls[0]
    assert adapter.get_ticker("ETH/EUR") is None


def test_btc_archive_paginates_without_duplication_or_interpolation():
    # Primera página llena; segunda página más corta con un hueco real.
    first = [_row(START + i * DAY) for i in range(1000)]
    second = [_row(START + 1000 * DAY), _row(START + 1002 * DAY)]
    starts = []

    def get(url):
        starts.append(int(url.split("start=")[1].split("&")[0]))
        return {"data": {"ohlc": first if len(starts) == 1 else second}}

    now = START + 1003 * DAY
    result = btc_daily_archive(http_get=get, now=now)
    assert result["available"] is True
    assert starts == [START, START + 1000 * DAY]
    assert result["count"] == 1002
    assert result["missing_days"] == 1
    assert result["candles"][-1]["t"] == START + 1002 * DAY
    assert result["complete_through"] == START + 1000 * DAY


def test_btc_archive_does_not_claim_partial_history_complete():
    def get(url):
        return {"data": {"ohlc": [_row(START + DAY)]}}

    result = btc_daily_archive(http_get=get, now=START + 2 * DAY)
    assert result["available"] is False
    assert result["candles"] == []


def test_eur_archive_continues_after_a_sparse_1000_day_window():
    starts = []

    def get(url):
        assert "/ohlc/btceur/" in url
        cursor = int(url.split("start=")[1].split("&")[0])
        starts.append(cursor)
        if cursor == START_EUR:
            return {"data": {"ohlc": [_row(START_EUR), _row(START_EUR + 2 * DAY)]}}
        return {"data": {"ohlc": [_row(START_EUR + 1000 * DAY)]}}

    result = btc_daily_archive(http_get=get, now=START_EUR + 1001 * DAY,
                               quote_currency="EUR")
    assert result["available"] is True
    assert result["symbol"] == "BTC/EUR"
    assert starts == [START_EUR, START_EUR + 1000 * DAY]
    assert result["count"] == 3
    assert result["missing_days"] == 998
    assert result["complete_through"] == START_EUR


def test_archive_does_not_claim_unreturned_recent_days_as_complete():
    rows = [_row(START + i * DAY) for i in range(3)]

    result = btc_daily_archive(http_get=lambda url: {"data": {"ohlc": rows}},
                               now=START + 8 * DAY)

    assert result["available"] is True
    assert result["last_candle_at"] == START + 2 * DAY
    assert result["complete_through"] == START + 2 * DAY


def test_public_archive_cache_cannot_be_modified_by_a_caller(tmp_path, monkeypatch):
    from app.marketdata import archive

    requests = []

    class PublicFixture:
        def __init__(self, http_get=None):
            assert http_get is None

        def get_candles(self, symbol, timeframe, limit, since):
            requests.append((symbol, timeframe, limit, since))
            return [{"t": START + i * DAY, "o": "4", "h": "6", "l": "3",
                     "c": "5", "v": "2", "closed": True} for i in range(3)]

    monkeypatch.setattr(archive, "BitstampAdapter", PublicFixture)
    monkeypatch.setattr(archive, "CACHE_DIR", tmp_path / "market-cache")
    monkeypatch.setattr(archive, "_cached", {})
    first = archive.btc_daily_archive(now=START + 8 * DAY)
    assert first["available"] is True
    first["candles"][0]["c"] = "999"
    first["candles"].append({"t": START + 3 * DAY})
    second = archive.btc_daily_archive(now=START + 8 * DAY)
    assert len(requests) == 1
    assert second["candles"][0]["c"] == "5"
    assert len(second["candles"]) == second["count"] == 3


def test_daily_archive_local_cache_survives_restart_and_labels_offline_copy(tmp_path, monkeypatch):
    from app.marketdata import archive

    monkeypatch.setattr(archive, "CACHE_DIR", tmp_path / "market-cache")
    monkeypatch.setattr(archive, "_cached", {})
    calls = []

    class Source:
        def __init__(self, http_get=None):
            assert http_get is None

        def get_candles(self, symbol, timeframe, limit, since):
            calls.append(since)
            return [{"t": START + i * DAY, "o": "4", "h": "6", "l": "3",
                     "c": "5", "v": "2", "closed": True} for i in range(3)]

    monkeypatch.setattr(archive, "BitstampAdapter", Source)
    first = archive.btc_daily_archive(now=START + 8 * DAY)
    assert first["available"] and len(calls) == 1
    assert (archive.CACHE_DIR / "btc-usd-daily-v1.json").is_file()

    class Offline(Source):
        def get_candles(self, symbol, timeframe, limit, since):
            calls.append("offline")
            return None

    monkeypatch.setattr(archive, "BitstampAdapter", Offline)
    monkeypatch.setattr(archive, "_cached", {})  # otro proceso, misma jornada
    current = archive.btc_daily_archive(now=START + 8 * DAY)
    assert current["available"] and current["cache_status"] == "local-current"
    assert calls == [START]
    current["candles"][0]["c"] = "999"

    monkeypatch.setattr(archive, "_cached", {})  # día siguiente, red caída
    stale = archive.btc_daily_archive(now=START + 9 * DAY)
    assert stale["available"] and stale["stale"] is True
    assert stale["cache_status"] == "local-stale"
    assert stale["last_candle_at"] == START + 2 * DAY
    assert stale["candles"][0]["c"] == "5"
    assert calls == [START, "offline"]


def test_daily_archive_rejects_corrupt_local_cache(tmp_path, monkeypatch):
    from app.marketdata import archive

    monkeypatch.setattr(archive, "CACHE_DIR", tmp_path / "market-cache")
    monkeypatch.setattr(archive, "_cached", {})

    class Source:
        def __init__(self, http_get=None):
            pass

        def get_candles(self, symbol, timeframe, limit, since):
            return [{"t": START + i * DAY, "o": "4", "h": "6", "l": "3",
                     "c": "5", "v": "2", "closed": True} for i in range(3)]

    monkeypatch.setattr(archive, "BitstampAdapter", Source)
    assert archive.btc_daily_archive(now=START + 8 * DAY)["available"]
    path = archive.CACHE_DIR / "btc-usd-daily-v1.json"
    import json
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    wrapper["result"]["candles"][0]["c"] = "999"
    path.write_text(json.dumps(wrapper), encoding="utf-8")

    class Offline(Source):
        def get_candles(self, symbol, timeframe, limit, since):
            return None

    monkeypatch.setattr(archive, "BitstampAdapter", Offline)
    monkeypatch.setattr(archive, "_cached", {})
    result = archive.btc_daily_archive(now=START + 9 * DAY)
    assert result["available"] is False and result["candles"] == []


def test_eur_archive_rejects_wrong_first_candle():
    def get(url):
        return {"data": {"ohlc": [_row(START_EUR + DAY)]}}

    result = btc_daily_archive(http_get=get, now=START_EUR + 2 * DAY,
                               quote_currency="EUR")
    assert result["available"] is False
    assert result["candles"] == []


def test_btc_archive_endpoint_and_studio_contract(client, monkeypatch):
    from app.marketdata import archive

    monkeypatch.setattr(archive, "btc_daily_archive", lambda **kwargs: {
        "available": True, "symbol": "BTC/" + kwargs.get("quote_currency", "USD"), "resolved_provider": "bitstamp",
        "timeframe": "1d", "candles": [_row(START)], "count": 1,
    })
    response = client.get("/market/btc-daily-archive")
    assert response.status_code == 200
    assert response.json()["resolved_provider"] == "bitstamp"
    assert client.get("/market/btc-daily-archive?quote_currency=EUR").json()["symbol"] == "BTC/EUR"
    assert client.get("/market/btc-daily-archive?quote_currency=JPY").status_code == 422
    page = client.get("/ui/").text
    assert "estudio.js?v=54" in page
    js = client.get("/ui/estudio.js").text
    assert 'id="st-range"' in js
    assert 'id="st-archive-year"' in js
    assert "/market/btc-daily-archive" in js
    assert 'showArchivePeriod($("st-range").value)' in js
    assert '<option value="10">10 años</option>' in js
    assert '<option value="all">Todo disponible</option>' in js
    assert 'chart.timeScale().setVisibleRange({ from: from, to: to })' in js
