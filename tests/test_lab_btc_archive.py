"""BTC 2012 entra en LAB como dataset local inmutable, no como orden."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.store import LabStore
from app.lab.engine import run_backtest, validate_candles, validate_strategy
from app.marketdata.archive import START
from app.marketdata.adapters.bitstamp import DAY


def _archive(*, gap=False):
    rows = [
        {"t": START + i * DAY, "o": "5", "h": "6", "l": "4", "c": "5", "v": "10", "closed": True}
        for i in range(5399)
    ]
    if gap:
        rows.pop(10)
    return {
        "available": True, "candles": rows,
        "first_candle_at": rows[0]["t"], "last_candle_at": rows[-1]["t"],
    }


def _client(tmp_path, *, initialize=True):
    if initialize:
        with LabStore(tmp_path / "lab.db").connect():
            pass
    app = FastAPI()
    app.include_router(create_lab_router(tmp_path / "lab.db"))
    return TestClient(app)


def test_import_btc_archive_over_5000_persists_hash_and_reuses_id(tmp_path, monkeypatch):
    from app.marketdata import archive

    monkeypatch.setattr(archive, "btc_daily_archive", _archive)
    client = _client(tmp_path)
    first = client.post("/lab/datasets/btc-archive")
    assert first.status_code == 200, first.text
    meta = first.json()
    assert meta["count"] == 5399
    assert meta["symbol"] == "BTC/USD" and meta["timeframe"] == "1d"
    assert meta["source"] == "bitstamp-btcusd-daily-public"
    assert meta["verified_public"] is True and meta["gaps"] == []
    assert client.post("/lab/datasets/btc-archive").json()["id"] == meta["id"]
    reopened = LabStore(tmp_path / "lab.db")
    saved, rows = reopened.dataset(meta["id"])
    assert saved["hash"] == meta["hash"]
    assert len(rows) == 5399 and rows[0]["t"] == START
    assert (tmp_path / "lab.db").stat().st_mode & 0o777 == 0o600
    strategy = reopened.create_strategy("Sin señal", validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.03", "take_profit": "0.08",
        "entry": {"kind": "RSI_LT", "period": 14, "threshold": "1"},
    }))
    bars, _ = validate_candles(rows, "1d", now=rows[-1]["t"] + 2 * DAY)
    run = reopened.save_run(meta["id"], strategy["version_id"],
                            run_backtest(bars, strategy["definition"], "1d", engine_version="lab-1.5"), "lab-1.5")
    assert run["dataset_hash"] == meta["hash"]
    assert run["dataset_source"] == meta["source"]


def test_import_btc_archive_rejects_incomplete_or_gapped_data(tmp_path, monkeypatch):
    from app.marketdata import archive

    client = _client(tmp_path)
    monkeypatch.setattr(archive, "btc_daily_archive", lambda: {
        "available": False, "reason": "La descarga falló", "candles": []})
    bad = client.post("/lab/datasets/btc-archive")
    assert bad.status_code == 422 and "descarga falló" in bad.text
    monkeypatch.setattr(archive, "btc_daily_archive", lambda: _archive(gap=True))
    gap = client.post("/lab/datasets/btc-archive")
    assert gap.status_code == 422 and "días sin vela" in gap.text
    assert client.get("/lab/datasets").json() == []


def test_import_btc_archive_rejects_offline_stale_cache_without_saving(tmp_path, monkeypatch):
    from app.marketdata import archive

    client = _client(tmp_path)
    stale = {**_archive(), "stale": True, "cache_status": "local-stale"}
    monkeypatch.setattr(archive, "btc_daily_archive", lambda: stale)
    response = client.post("/lab/datasets/btc-archive")
    assert response.status_code == 422
    assert "desactualizado" in response.text
    assert client.get("/lab/datasets").json() == []

    fresh = {**_archive(), "cache_status": "local-current"}
    monkeypatch.setattr(archive, "btc_daily_archive", lambda: fresh)
    imported = client.post("/lab/datasets/btc-archive")
    assert imported.status_code == 200 and imported.json()["verified_public"] is True


def test_import_btc_archive_requires_local_origin(tmp_path, monkeypatch):
    from app.marketdata import archive

    monkeypatch.setattr(archive, "btc_daily_archive", _archive)
    client = _client(tmp_path, initialize=False)
    response = client.post("/lab/datasets/btc-archive", headers={"Origin": "https://example.org"})
    assert response.status_code == 403
    assert not (tmp_path / "lab.db").exists()
