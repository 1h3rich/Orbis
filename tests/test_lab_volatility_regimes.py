"""Regímenes descriptivos LAB: ventana causal y etiquetas futuras separadas."""

from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.engine import LabError, validate_candles
from app.lab.store import LabStore
from app.lab.volatility_regimes import analyze_volatility_regimes


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
STEP = 3600


def _case(stamp: int, outcome: str | None = "1", *, four_hour: str | None = None) -> dict:
    return {"features_at_close": {"signal_time": stamp},
            "future_labels": {"outcomes_pct": {str(h): outcome if h == 3600 else
                                               four_hour if h == 14400 else None
                                               for h in (3600, 14400, 86400, 604800)}}}


def test_regime_uses_only_prior_closes_and_restarts_after_gap():
    values = ["100"] * 31 + ["110" if i % 2 else "100" for i in range(30)]
    candles = [{"t": START + i * STEP, "c": close} for i, close in enumerate(values)]
    candles.extend({"t": START + (i + 4) * STEP, "c": "100"} for i in range(61, 65))
    cases = [_case(candles[1]["t"]), _case(candles[25]["t"], "2"),
             _case(candles[55]["t"], "-1"), _case(candles[-1]["t"])]
    report = analyze_volatility_regimes(candles, cases, "1h", lookback=20,
                                        threshold_pct="2")
    assert report["regimes"]["BELOW"]["cases"] == 1
    assert report["regimes"]["AT_OR_ABOVE"]["cases"] == 1
    assert report["regimes"]["INSUFFICIENT_HISTORY"]["cases"] == 2
    assert report["regimes"]["BELOW"]["horizons"]["3600"]["mean_pct"] == "2"
    assert report["regimes"]["AT_OR_ABOVE"]["horizons"]["3600"]["positive_rate"] == "0"
    assert report["regimes"]["BELOW"]["horizons"]["86400"]["count"] == 0
    changed_future = [*candles[:26], *[{**bar, "c": "999"} for bar in candles[26:]]]
    early = analyze_volatility_regimes(candles[:26], [cases[1]], "1h", lookback=20)
    after = analyze_volatility_regimes(changed_future, [cases[1]], "1h", lookback=20)
    assert {name: group["cases"] for name, group in early["regimes"].items()} == {
        name: group["cases"] for name, group in after["regimes"].items()}
    for key in ("count", "mean_pct", "non_overlapping"):
        assert early["regimes"]["BELOW"]["horizons"]["3600"][key] == (
            after["regimes"]["BELOW"]["horizons"]["3600"][key])
    with pytest.raises(LabError, match="Umbral"):
        analyze_volatility_regimes(candles, cases, "1h", threshold_pct="NaN")


def test_non_overlapping_subset_is_selected_globally_across_regimes():
    candles = [{"t": START + i * STEP, "c": "100" if i < 11 else "110"}
               for i in range(15)]
    # La señal baja sucede antes de la alta, aunque los casos lleguen invertidos.
    cases = [_case(candles[11]["t"], four_hour="-3"),
             _case(candles[10]["t"], four_hour="2")]
    report = analyze_volatility_regimes(candles, cases, "1h", lookback=2,
                                        threshold_pct="2")
    below = report["regimes"]["BELOW"]["horizons"]["14400"]
    above = report["regimes"]["AT_OR_ABOVE"]["horizons"]["14400"]
    assert below["count"] == above["count"] == 1
    assert below["non_overlapping"] == {"count": 1, "mean_pct": "2",
                                        "median_pct": "2", "positive_rate": "1"}
    assert above["non_overlapping"]["count"] == 0
    assert report["version"] == "volatility-regimes-1.2"


def test_chronological_reserve_embargoes_future_labels_at_boundary():
    candles = [{"t": START + i * STEP, "c": "100"} for i in range(15)]
    # Corte al índice 10; el caso del índice 6 llega exactamente al corte.
    cases = [_case(candles[5]["t"], four_hour="1"),
             _case(candles[6]["t"], four_hour="9"),
             _case(candles[10]["t"], four_hour="-2")]
    report = analyze_volatility_regimes(candles, cases, "1h", lookback=2,
                                        train_fraction="0.7")
    assert report["chronological_reserve"]["cutoff_t"] == candles[10]["t"]
    assert report["chronological_reserve"]["embargoed_cases_by_horizon"]["14400"] == 1
    stats = report["regimes"]["BELOW"]["horizons"]["14400"]
    assert stats["count"] == 3
    assert stats["development"]["count"] == 1
    assert stats["development"]["mean_pct"] == "1"
    assert stats["out_of_sample"]["count"] == 1
    assert stats["out_of_sample"]["mean_pct"] == "-2"
    assert stats["development"]["non_overlapping"]["count"] == 1
    assert stats["out_of_sample"]["non_overlapping"]["count"] == 1
    assert report["chronological_reserve"]["status"] == "EXPLORATORY_NOT_SEALED"
    with pytest.raises(LabError, match="Fracción"):
        analyze_volatility_regimes(candles, cases, "1h", train_fraction="0.95")


def test_regime_api_requires_verified_research_and_does_not_write(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    prices = ["100", "99", "98", "97", "96", "95", "94", "93", "92", "91", "90", "89"]
    raw = [{"t": START + i * STEP, "o": str(Decimal(close) + 1), "h": str(Decimal(close) + 2),
            "l": str(Decimal(close) - 1), "c": close, "v": str(10 + i)}
           for i, close in enumerate(prices)]
    bars, gaps = validate_candles(raw, "1h", now=START + 20 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    created = client.post("/lab/research/runs", json={
        "dataset_id": dataset["id"], "rule": {"kind": "BEARISH_STREAK", "period": 3},
    })
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    before = store.research_run(run_id)
    response = client.get(f"/lab/research/runs/{run_id}/volatility-regimes",
                          params={"lookback": 2, "threshold_pct": "0.1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["environment"] == "LAB" and body["run_id"] == run_id
    assert body["dataset_hash"] == dataset["hash"]
    assert body["verification"]["verified"] is True
    assert body["cases"] == before["summary"]["cases"]
    assert body["regimes"]["INSUFFICIENT_HISTORY"]["cases"] == 0
    assert body["comparison_basis"] == "DESCRIPTIVE_NOT_INDEPENDENT"
    assert body["chronological_reserve"]["status"] == "EXPLORATORY_NOT_SEALED"
    selected = client.get(f"/lab/research/runs/{run_id}/volatility-regimes",
                          params={"train_fraction": "0.6"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["chronological_reserve"]["train_fraction"] == "0.6"
    assert response.headers["cache-control"] == "no-store"
    assert store.research_run(run_id) == before
    invalid = client.get(f"/lab/research/runs/{run_id}/volatility-regimes",
                         params={"threshold_pct": "NaN"})
    assert invalid.status_code == 422
    invalid_fraction = client.get(f"/lab/research/runs/{run_id}/volatility-regimes",
                                  params={"train_fraction": "0.95"})
    assert invalid_fraction.status_code == 422
    with sqlite3.connect(path) as db:
        db.execute("UPDATE research_events SET event_json='{}' WHERE run_id=? AND index_no=0",
                   (run_id,))
    corrupted = client.get(f"/lab/research/runs/{run_id}/volatility-regimes")
    assert corrupted.status_code == 409, corrupted.text
