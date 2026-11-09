"""Parámetros elegidos sin leer la reserva, con persistencia y migración."""

from app.lab.schema import CURRENT_SCHEMA_VERSION
from decimal import Decimal
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import LabError, run_backtest, validate_candles, validate_strategy
from app.lab.optimization import (OPTIMIZER_VERSION, LEGACY_WALK_FORWARD_OPTIMIZER_VERSION,
                                  WALK_FORWARD_OPTIMIZER_VERSION,
                                  optimize_stops, walk_forward_stops)
from app.lab.store import LabStore


START = 1_735_689_600


def history(count=200):
    return validate_candles([
        {"t": START + i * 3600, "o": str(100 + i), "h": str(102 + i),
         "l": str(99 + i), "c": str(101 + i), "v": "10"}
        for i in range(count)
    ], "1h", now=START + (count + 2) * 3600)[0]


def strategy():
    return validate_strategy({"direction": "LONG", "initial_capital": "1000",
        "allocation": "0.2", "fee_rate": "0.001", "slippage_rate": "0.001",
        "stop_loss": "0.03", "take_profit": "0.08",
        "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 10}})


def test_grid_selects_on_train_only_and_keeps_version_unchanged():
    bars = history()
    definition = strategy()
    result = optimize_stops(bars, definition, "1h", ["0.02", "0.04"], ["0.05", "0.1"], "0.7")
    assert definition == strategy()
    assert result["optimizer_version"] == OPTIMIZER_VERSION
    assert result["split_index"] == 140
    assert len(result["candidates"]) == 4
    assert result["selected"] is not None
    assert result["selected"]["eligible"] is True
    assert result["selected"]["train_metrics"]["trades"] >= result["min_train_trades"]
    sensitivity = result["sensitivity"]
    assert sensitivity["neighbor_count"] == 2
    assert len(sensitivity["neighbors"]) == 2
    assert sensitivity["eligible_neighbors"] == sum(row["eligible"] for row in sensitivity["neighbors"])
    assert sensitivity["positive_neighbors"] == sum(
        row["eligible"] and Decimal(row["net_profit"]) > 0 for row in sensitivity["neighbors"])
    assert all(sum((row["stop_loss"] != result["selected"]["stop_loss"],
                    row["take_profit"] != result["selected"]["take_profit"])) == 1
               for row in sensitivity["neighbors"])
    assert Decimal(result["selected"]["train_metrics"]["net_profit"]) == max(
        Decimal(item["train_metrics"]["net_profit"]) for item in result["candidates"] if item["eligible"])
    assert len(result["out_of_sample"]["equity"]) == 60
    assert all(event["signal_time"] >= bars[140].t for event in result["out_of_sample"]["events"])

    changed = list(bars)
    last = bars[-1]
    changed[-1] = type(last)(last.t, last.o, last.o, last.o / 2, last.o / 2, last.v)
    rerun = optimize_stops(changed, definition, "1h", ["0.02", "0.04"], ["0.05", "0.1"], "0.7")
    assert rerun["candidates"] == result["candidates"]
    assert rerun["selected"] == result["selected"]
    assert rerun["sensitivity"] == sensitivity
    assert rerun["out_of_sample"]["metrics"] != result["out_of_sample"]["metrics"]


def test_empty_sample_and_invalid_grid_never_peek_at_reserve():
    quiet = validate_strategy({**strategy(), "entry": {"kind": "RSI_LT", "period": 14, "threshold": "1"}})
    result = optimize_stops(history(60), quiet, "1h", ["0.02"], ["0.05"], "0.5")
    assert result["selected"] is None and result["out_of_sample"] is None
    assert result["sensitivity"] is None
    assert "reserva no evaluada" in result["warnings"][-1]
    with pytest.raises(LabError, match="duplicados"):
        optimize_stops(history(), strategy(), "1h", ["0.02", "0.020"], ["0.05"])
    with pytest.raises(LabError, match="entre 1 y 3"):
        optimize_stops(history(), strategy(), "1h", ["0.02"] * 4, ["0.05"])
    with pytest.raises(LabError, match="fuera de rango"):
        optimize_stops(history(), strategy(), "1h", ["1"], ["0.05"])
    with pytest.raises(LabError, match="Versión del optimizador"):
        optimize_stops(history(), strategy(), "1h", ["0.02"], ["0.05"], optimizer_version=[])
    with pytest.raises(LabError, match="Versión del optimizador"):
        optimize_stops(history(), strategy(), "1h", ["0.02"], ["0.05"],
                       optimizer_version=WALK_FORWARD_OPTIMIZER_VERSION)


def test_single_point_grid_does_not_claim_parameter_robustness():
    result = optimize_stops(history(), strategy(), "1h", ["0.02"], ["0.05"])
    assert result["selected"] is not None
    assert result["sensitivity"] == {
        "neighbor_count": 0, "eligible_neighbors": 0, "positive_neighbors": 0,
        "worst_neighbor_net_profit": None, "neighbors": []}
    assert any("no hay sensibilidad" in warning for warning in result["warnings"])


def test_legacy_grid_replays_without_new_sensitivity_field(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars = history()
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    version = store.create_strategy("EMA", strategy())
    legacy = optimize_stops(bars, strategy(), "1h", ["0.02", "0.04"],
                            ["0.05", "0.1"], "0.7", engine_version=ENGINE_VERSION,
                            optimizer_version="grid-stop-target-1.0")
    assert "sensitivity" not in legacy
    saved = store.save_optimization(dataset["id"], version["version_id"],
                                    dataset["hash"], legacy, ENGINE_VERSION)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    check = TestClient(app).get(f"/lab/optimizations/{saved['id']}/verify").json()
    assert check["status"] == "verified" and all(check["checks"].values())


def test_walk_forward_reselects_without_peeking_at_its_reserve():
    bars = history(600)
    result = walk_forward_stops(bars, strategy(), "1h", ["0.02", "0.04"],
                                ["0.05", "0.1"], 3,
                                optimizer_version=LEGACY_WALK_FORWARD_OPTIMIZER_VERSION)
    assert result["optimizer_version"] == LEGACY_WALK_FORWARD_OPTIMIZER_VERSION
    assert result["method"] == "WALK_FORWARD_GRID_STOP_TARGET"
    assert result["eligible_folds"] == 3
    assert [window["train_bars"] for window in result["windows"]] == [150, 300, 450]
    assert [window["test_bars"] for window in result["windows"]] == [150, 150, 150]
    assert [window["test_start_t"] for window in result["windows"]] == [bars[i].t for i in (150, 300, 450)]
    assert all(window["out_of_sample"]["events"][0]["signal_time"] >= window["test_start_t"]
               for window in result["windows"] if window["out_of_sample"]["events"])
    assert result["tested_trades"] == sum(window["out_of_sample"]["metrics"]["trades"]
                                           for window in result["windows"])
    changed = list(bars)
    last = bars[-1]
    changed[-1] = type(last)(last.t, last.o, last.o, last.o / 2, last.o / 2, last.v)
    rerun = walk_forward_stops(changed, strategy(), "1h", ["0.02", "0.04"],
                               ["0.05", "0.1"], 3,
                               optimizer_version=LEGACY_WALK_FORWARD_OPTIMIZER_VERSION)
    assert rerun["windows"][:2] == result["windows"][:2]
    assert rerun["windows"][2]["candidates"] == result["windows"][2]["candidates"]
    assert rerun["windows"][2]["selected"] == result["windows"][2]["selected"]
    assert rerun["windows"][2]["out_of_sample"]["metrics"] != result["windows"][2]["out_of_sample"]["metrics"]


def test_walk_forward_final_reserve_is_not_used_to_choose_parameters():
    bars = history(600)
    result = walk_forward_stops(bars, strategy(), "1h", ["0.02", "0.04"],
                                ["0.05", "0.1"], 3)
    assert result["optimizer_version"] == WALK_FORWARD_OPTIMIZER_VERSION
    assert result["development_bars"] == 480
    assert [window["test_bars"] for window in result["windows"]] == [120, 120, 120]
    assert result["windows"][-1]["test_end_t"] == bars[479].t
    final = result["final_holdout"]
    assert final["start_t"] == bars[480].t and final["end_t"] == bars[-1].t
    assert final["bars"] == 120 and final["selected"] is not None
    assert final["out_of_sample"] is not None
    changed = list(bars)
    last = bars[-1]
    changed[-1] = type(last)(last.t, last.o, last.o, last.o / 2, last.o / 2, last.v)
    rerun = walk_forward_stops(changed, strategy(), "1h", ["0.02", "0.04"],
                               ["0.05", "0.1"], 3)
    assert rerun["windows"] == result["windows"]
    assert rerun["final_holdout"]["candidates"] == final["candidates"]
    assert rerun["final_holdout"]["selected"] == final["selected"]
    assert rerun["final_holdout"]["out_of_sample"]["metrics"] != final["out_of_sample"]["metrics"]


def test_walk_forward_persists_verifies_and_rejects_ignored_holdout_fraction(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars = history(600)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    version = store.create_strategy("EMA", strategy())
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    body = {"dataset_id": dataset["id"], "version_id": version["version_id"],
            "stop_losses": ["0.02", "0.04"], "take_profits": ["0.05", "0.1"], "folds": 3}
    assert client.post("/lab/optimizations/walk-forward", json={**body, "train_fraction": "0.7"}).status_code == 422
    response = client.post("/lab/optimizations/walk-forward", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["optimizer_version"] == WALK_FORWARD_OPTIMIZER_VERSION
    assert saved["eligible_folds"] == 3
    assert saved["final_holdout"]["bars"] == 120
    assert len(client.get("/lab/optimizations").json()) == 1
    assert client.get(f"/lab/optimizations/{saved['id']}").json() == saved
    check = client.get(f"/lab/optimizations/{saved['id']}/verify").json()
    assert check["status"] == "verified" and all(check["checks"].values())
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    assert TestClient(reopened).get(f"/lab/optimizations/{saved['id']}/verify").json()["status"] == "verified"


def test_legacy_walk_forward_report_still_replays_after_new_version(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars = history(600)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    version = store.create_strategy("EMA", strategy())
    legacy = walk_forward_stops(bars, strategy(), "1h", ["0.02", "0.04"],
                                ["0.05", "0.1"], 3,
                                engine_version=ENGINE_VERSION,
                                optimizer_version=LEGACY_WALK_FORWARD_OPTIMIZER_VERSION)
    assert "final_holdout" not in legacy
    saved = store.save_optimization(dataset["id"], version["version_id"],
                                    dataset["hash"], legacy, ENGINE_VERSION)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    check = TestClient(app).get(f"/lab/optimizations/{saved['id']}/verify").json()
    assert check["status"] == "verified" and all(check["checks"].values())


def test_walk_forward_rejects_insufficient_windows_and_bad_grid():
    quiet = validate_strategy({**strategy(), "entry": {"kind": "RSI_LT", "period": 14, "threshold": "1"}})
    no_signal = walk_forward_stops(history(120), quiet, "1h", ["0.02"], ["0.05"], 3,
                                   optimizer_version=LEGACY_WALK_FORWARD_OPTIMIZER_VERSION)
    assert no_signal["eligible_folds"] == 0 and no_signal["tested_trades"] == 0
    assert all(window["selected"] is None and window["out_of_sample"] is None
               for window in no_signal["windows"])
    with pytest.raises(LabError, match="30 velas"):
        walk_forward_stops(history(119), strategy(), "1h", ["0.02"], ["0.05"], 3,
                           optimizer_version=LEGACY_WALK_FORWARD_OPTIMIZER_VERSION)
    with pytest.raises(LabError, match="2 y 5"):
        walk_forward_stops(history(), strategy(), "1h", ["0.02"], ["0.05"], True)
    with pytest.raises(LabError, match="duplicados"):
        walk_forward_stops(history(), strategy(), "1h", ["0.02", "0.020"], ["0.05"], 3)
    with pytest.raises(LabError, match="30 velas"):
        walk_forward_stops(history(149), strategy(), "1h", ["0.02"], ["0.05"], 3)
    with pytest.raises(LabError, match="10 % y 40 %"):
        walk_forward_stops(history(200), strategy(), "1h", ["0.02"], ["0.05"], 3,
                           final_holdout_fraction="0.5")


def test_api_saves_grid_and_v4_migrates_without_losing_prior_results(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars = history()
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    version = store.create_strategy("EMA", strategy())
    prior = store.save_run(dataset["id"], version["version_id"], run_backtest(bars, strategy(), engine_version="lab-1.5"), "lab-1.5")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE perp_optimization_variants")
        db.execute("DROP TABLE perp_optimizations")
        db.execute("DROP TABLE perp_rule_observations")
        db.execute("DROP TABLE perp_validations")
        db.execute("DROP TABLE perp_runs")
        db.execute("DROP TABLE perp_profiles")
        db.execute("DROP TABLE perp_datasets")
        db.execute("DROP TABLE ai_analyses")
        db.execute("DROP TABLE paper_gap_reports")
        db.execute("DROP TABLE dataset_refreshes")
        db.execute("DROP TABLE strategy_imports")
        db.execute("DROP TABLE optimization_runs")
        db.execute("PRAGMA user_version=4")
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    body = {"dataset_id": dataset["id"], "version_id": version["version_id"],
            "stop_losses": ["0.02", "0.04"], "take_profits": ["0.05", "0.1"],
            "train_fraction": "0.7"}
    response = client.post("/lab/optimizations", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["dataset_hash"] == dataset["hash"]
    assert result["engine_version"] == ENGINE_VERSION
    assert result["optimizer_version"] == OPTIMIZER_VERSION
    assert len(result["strategy_hash"]) == len(result["result_hash"]) == 64
    check = client.get("/lab/optimizations/" + result["id"] + "/verify").json()
    assert check["status"] == "verified" and all(check["checks"].values())
    assert len(client.get("/lab/optimizations").json()) == 1
    assert client.get("/lab/optimizations/" + result["id"]).json() == result
    assert LabStore(path).version(version["version_id"])["definition"] == strategy()
    assert LabStore(path).run(prior["id"]) == prior
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    assert TestClient(reopened).get("/lab/optimizations/" + result["id"]).json() == result
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert client.get("/lab/optimizations", headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_optimization_verifier_rejects_result_or_strategy_changes(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", history(), [])
    version = store.create_strategy("EMA", strategy())
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    saved = client.post("/lab/optimizations", json={
        "dataset_id": dataset["id"], "version_id": version["version_id"],
        "stop_losses": ["0.02", "0.04"], "take_profits": ["0.05", "0.1"],
        "train_fraction": "0.7",
    }).json()
    uri = f"/lab/optimizations/{saved['id']}/verify"
    assert client.get(uri).json()["status"] == "verified"
    store.add_version(version["id"], validate_strategy({**strategy(), "stop_loss": "0.04"}))
    assert client.get(uri).json()["status"] == "verified"
    with sqlite3.connect(path) as db:
        result = json.loads(db.execute("SELECT result_json FROM optimization_runs WHERE id=?",
                                       (saved["id"],)).fetchone()[0])
        result["candidates"][0]["train_metrics"]["net_profit"] = "999999"
        db.execute("UPDATE optimization_runs SET result_json=? WHERE id=?",
                   (json.dumps(result), saved["id"]))
    assert client.get(uri).json()["checks"]["stored_result"] is False
    with sqlite3.connect(path) as db:
        db.execute("UPDATE strategy_versions SET definition_json=? WHERE id=?",
                   (json.dumps({**strategy(), "initial_capital": "999"}), version["version_id"]))
    assert client.get(uri).json()["checks"]["strategy"] is False
