"""Históricos GRVT TRADE cerrados, paginación y frontera Spot/PERP de LAB."""

from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router, download_grvt
from app.lab.store import LabStore
from app.marketdata.adapters.grvt import GrvtAdapter, GrvtHistoryError
from app.marketdata.grvt_candle_time import GrvtCandleNotClosed


START = 1_735_689_600  # 2025-01-01 00:00 UTC
STEP = 3600
END = START + 4 * STEP
SYMBOL = "BTC/USDT:PERP"
NATIVE = "BTC_USDT_Perp"


def bar(index, *, instrument=NATIVE, close=None):
    price = 100 + index
    timestamp = START + index * STEP
    return {"instrument": instrument, "open_time": str(timestamp * 1_000_000_000),
            "close_time": str((timestamp + STEP) * 1_000_000_000),
            "open": str(price), "high": str(price + 1), "low": str(price - 1),
            "close": str(close if close is not None else price), "volume_b": "10"}


def test_grvt_reverse_cursor_download_returns_exact_closed_range():
    calls = []

    def post(url, body):
        calls.append((url, deepcopy(body)))
        if body["cursor"] == "":
            return {"result": [bar(3), bar(2)], "next": "older"}
        assert body["cursor"] == "older"
        return {"result": [bar(1), bar(0)], "next": ""}

    adapter = GrvtAdapter(http_post=post)
    candles = adapter.get_historical_candles(SYMBOL, "1h", START, END, now=END)
    assert [item["t"] for item in candles] == [START + i * STEP for i in range(4)]
    assert all(item["v"] == "10" for item in candles)
    assert len(calls) == 2
    assert calls[0] == ("https://market-data.grvt.io/full/v1/kline", {
        "instrument": NATIVE, "interval": "CI_1_H", "type": "TRADE",
        "start_time": str(START * 1_000_000_000),
        "end_time": str(END * 1_000_000_000 - 1), "limit": 4, "cursor": ""})
    assert calls[1][1]["cursor"] == "older"


@pytest.mark.parametrize("pages,reason", [
    ([{"result": [bar(3), bar(2)], "next": ""}], "incompleto"),
    ([{"result": [bar(3, instrument="ETH_USDT_Perp")], "next": "x"}], "otro instrumento"),
    ([{"result": [bar(3), bar(2)], "next": "again"},
      {"result": [bar(3), bar(2)], "next": "again"}], "cursor repetido"),
    ([{"result": [bar(3), bar(2)], "next": "older"},
      {"result": [bar(2, close="999"), bar(1), bar(0)], "next": ""}], "cambió"),
])
def test_grvt_history_fails_closed_on_bad_pages(pages, reason):
    responses = iter(pages)
    adapter = GrvtAdapter(http_post=lambda _url, _body: next(responses))
    with pytest.raises(GrvtHistoryError, match=reason):
        adapter.get_historical_candles(SYMBOL, "1h", START, END, now=END)


def test_grvt_history_rejects_open_bar_wrong_symbol_and_oversized_request():
    adapter = GrvtAdapter(http_post=lambda _url, _body: pytest.fail("No debe consultar la red"))
    for symbol, start, end, now in (
        ("BTC/USDT", START, END, END),
        (SYMBOL, START, END, END - 1),
        (SYMBOL, START, START + 5001 * STEP, START + 5001 * STEP),
    ):
        with pytest.raises(GrvtHistoryError):
            adapter.get_historical_candles(symbol, "1h", start, end, now=now)


def test_grvt_history_rejects_float_prices_before_lab_can_round_them():
    wrong = bar(3)
    wrong["close"] = 103.1
    adapter = GrvtAdapter(http_post=lambda _url, _body: {"result": [wrong], "next": ""})
    with pytest.raises(GrvtHistoryError, match="cifras textuales"):
        adapter.get_historical_candles(SYMBOL, "1h", START, END, now=END)


@pytest.mark.parametrize("kind", ["TRADE", "MARK"])
@pytest.mark.parametrize("index, message", [(3, "pendiente de cierre"), (1, "histórica incompleta")])
def test_grvt_history_rejects_nonfinal_snapshot_in_closed_range(kind, index, message):
    rows = [bar(i) for i in reversed(range(4))]
    row = rows[3 - index]
    row["close_time"] = str(int(row["close_time"]) - 1)
    adapter = GrvtAdapter(http_post=lambda *_: {"result": rows, "next": ""})
    download = adapter.get_historical_candles if kind == "TRADE" else adapter.get_mark_history

    with pytest.raises(GrvtHistoryError, match=message):
        download(SYMBOL, "1h", START, END, now=END)
    row["close_time"] = bar(index)["close_time"]
    assert [item["t"] for item in download(SYMBOL, "1h", START, END, now=END)] == list(range(START, END, STEP))


def test_lab_download_never_stores_unconfirmed_grvt_candle(tmp_path, monkeypatch):
    rows = [bar(i) for i in reversed(range(4))]
    rows[0]["close_time"] = str((END - 1) * 1_000_000_000)
    adapter = GrvtAdapter(http_post=lambda *_: {"result": rows, "next": ""})
    monkeypatch.setattr("app.lab.api.GrvtAdapter", lambda: adapter)
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "grvt lab partial candle password"}).status_code == 200
    request = {"provider": "grvt", "symbol": SYMBOL, "timeframe": "1h", "start": START, "end": END}

    assert client.post("/lab/datasets/download", json=request).status_code == 422
    assert LabStore(path).datasets() == []
    rows[0]["close_time"] = bar(3)["close_time"]
    assert client.post("/lab/datasets/download", json=request).status_code == 200
    assert len(LabStore(path).datasets()) == 1


@pytest.mark.parametrize("kind", ["TRADE", "MARK"])
def test_only_missing_latest_grvt_candle_is_reported_as_pending(kind):
    rows = [bar(i) for i in (2, 1, 0)]
    adapter = GrvtAdapter(http_post=lambda *_: {"result": rows, "next": ""})
    download = adapter.get_historical_candles if kind == "TRADE" else adapter.get_mark_history
    with pytest.raises(GrvtCandleNotClosed) as pending:
        download(SYMBOL, "1h", START, END, now=END)
    assert pending.value.closes_at_ns == END * 1_000_000_000
    rows.pop(1)
    rows.insert(0, {**bar(3), "close_time": str(END * 1_000_000_000 - 1)})
    with pytest.raises(GrvtHistoryError, match="incompleto") as gap:
        download(SYMBOL, "1h", START, END, now=END)
    assert not isinstance(gap.value, GrvtCandleNotClosed)


def test_grvt_api_persists_source_hash_and_uses_same_lab_signal_engine(tmp_path, monkeypatch):
    class Source:
        known_pairs = (SYMBOL,)

        def get_historical_candles(self, symbol, timeframe, start, end, *, now):
            assert (symbol, timeframe, start, end) == (SYMBOL, "1h", START, END)
            return [{"t": START + i * STEP, "o": str(100 + i), "h": str(101 + i),
                     "l": str(99 + i), "c": str(100 + i), "v": "10"} for i in range(4)]

    monkeypatch.setattr("app.lab.api.GrvtAdapter", Source)
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "grvt lab history test password"}).status_code == 200
    body = {"provider": "grvt", "symbol": SYMBOL, "timeframe": "1h", "start": START, "end": END}
    first = client.post("/lab/datasets/download", json=body)
    assert first.status_code == 200, first.text
    dataset = first.json()
    assert dataset["symbol"] == SYMBOL
    assert dataset["source"] == "grvt-perp-trade-public"
    assert dataset["count"] == 4 and dataset["gaps"] == []
    assert client.post("/lab/datasets/download", json=body).json()["id"] == dataset["id"]
    assert LabStore(path).dataset(dataset["id"])[0]["hash"] == dataset["hash"]

    definition = {"direction": "LONG", "initial_capital": "1000", "allocation": "1",
                  "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1", "take_profit": "0.2",
                  "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}
    strategy = client.post("/lab/strategies", json={"name": "GRVT tendencia", "definition": definition}).json()
    run = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                          "version_id": strategy["version_id"]})
    assert run.status_code == 200, run.text
    assert run.json()["events"][0]["signal_time"] == START + STEP
    assert run.json()["trades"][0]["entry_time"] == START + 2 * STEP
    assert client.post("/lab/datasets/download", json={**body, "symbol": "BTC/USDT"}).status_code == 422
    assert client.post("/lab/datasets/download", json={**body, "provider": "binance"}).status_code == 422


def test_grvt_api_does_not_store_partial_or_gapped_history(tmp_path, monkeypatch):
    class Source:
        known_pairs = (SYMBOL,)

        def get_historical_candles(self, symbol, timeframe, start, end, *, now):
            return [{"t": START + i * STEP, "o": "100", "h": "101", "l": "99", "c": "100", "v": "10"}
                    for i in (0, 1, 3)]

    monkeypatch.setattr("app.lab.api.GrvtAdapter", Source)
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "grvt lab partial test password"}).status_code == 200
    response = client.post("/lab/datasets/download", json={
        "provider": "grvt", "symbol": SYMBOL, "timeframe": "1h", "start": START, "end": END})
    assert response.status_code == 422
    assert LabStore(path).datasets() == []


def test_download_grvt_requires_perpetual_identity_without_network():
    class Source:
        known_pairs = (SYMBOL,)

        def get_historical_candles(self, *_args, **_kwargs):
            pytest.fail("No debe descargar Spot desde GRVT")

    with pytest.raises(Exception, match="perpetuo explícito"):
        download_grvt("BTC/USDT", "1h", START, END, adapter=Source(), now=END)
    with pytest.raises(Exception, match="Temporalidad no admitida"):
        download_grvt(SYMBOL, "1w", START, END, adapter=Source(), now=END)
