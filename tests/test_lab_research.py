"""Investigación sin estrategia: causalidad, distribuciones y migración LAB."""

from app.lab.schema import CURRENT_SCHEMA_VERSION
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.engine import LabError, evaluate, supports_engine_version, validate_candles, validate_rule
from app.lab.research import run_research
from app.lab.store import LabStore, content_hash


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
STEP = 3600


def rows():
    prices = [(100, 99), (99, 98), (98, 97), (97, 96), (96, 97), (97, 96),
              (96, 95), (95, 94), (94, 95), (95, 96), (96, 97), (97, 98)]
    return [{"t": START + i * STEP, "o": str(open_price),
             "h": str(max(open_price, close_price) + 1),
             "l": str(min(open_price, close_price) - 1),
             "c": str(close_price), "v": str(10 + i)}
            for i, (open_price, close_price) in enumerate(prices)]


def structure():
    return {"op": "AND", "children": [
        {"kind": "BEARISH_STREAK", "period": 3},
        {"kind": "VOLUME_RISING_STREAK", "period": 3},
    ]}


def test_research_finds_each_structure_and_excludes_incomplete_horizons():
    bars, _ = validate_candles(rows(), "1h", now=START + 20 * STEP)
    rule = validate_rule(structure())
    result = run_research(bars, rule, "1h")
    assert [event["signal_time"] for event in result["events"]] == [START + i * STEP for i in (2, 3, 7)]
    assert result["summary"]["cases"] == 3
    one_hour = result["summary"]["horizons"]["3600"]
    assert one_hour["count"] == 3
    assert Decimal(one_hour["min_pct"]) < 0 < Decimal(one_hour["max_pct"])
    assert result["summary"]["horizons"]["86400"]["count"] == 0
    assert result["summary"]["horizons"]["86400"]["mean_pct"] is None
    assert result["events"][0]["explanation"]["children"][0]["matched"] is True
    assert result["events"][0]["max_up_pct"] is not None
    assert result["events"][0]["full_7d_followup"] is False
    assert [event["signal_time"] for event in run_research(bars[:4], rule, "1h")["events"]] == [
        START + 2 * STEP, START + 3 * STEP]
    assert evaluate(rule, bars, 2)[0] == evaluate(rule, bars[:3], 2)[0]
    with pytest.raises(LabError, match="2 y 10"):
        validate_rule({"kind": "BEARISH_STREAK", "period": 11})


def test_bullish_and_volume_streaks_are_causal_and_require_full_window():
    bars, _ = validate_candles(rows(), "1h", now=START + 20 * STEP)
    bullish = validate_rule({"kind": "BULLISH_STREAK", "period": 3})
    volume = validate_rule({"kind": "VOLUME_RISING_STREAK", "period": 3})
    assert [i for i in range(len(bars)) if evaluate(bullish, bars, i)[0]] == [10, 11]
    assert [i for i in range(len(bars)) if evaluate(volume, bars, i)[0]] == list(range(2, len(bars)))
    assert evaluate(volume, bars, 1)[0] is False
    assert evaluate(bullish, bars, 10) == evaluate(bullish, bars[:11], 10)
    changed_future = [*bars[:11], bars[0]]
    assert evaluate(bullish, changed_future, 10) == evaluate(bullish, bars, 10)


def test_ascending_volume_counts_only_the_selected_candles_and_keeps_legacy_rule():
    raw = rows()[:5]
    for row, volume in zip(raw, ("50", "10", "20", "30", "30")):
        row["v"] = volume
    bars, _ = validate_candles(raw, "1h", now=START + 6 * STEP)
    corrected = validate_rule({"kind": "VOLUME_ASCENDING_BARS", "period": 3})
    legacy = validate_rule({"kind": "VOLUME_RISING_STREAK", "period": 3})
    assert evaluate(corrected, bars, 3)[0] is True  # 10 < 20 < 30
    assert evaluate(corrected, bars[:4], 3) == evaluate(corrected, bars, 3)
    assert evaluate(corrected, bars, 2)[0] is False  # 50 < 10 es falso
    assert evaluate(corrected, bars, 4)[0] is False  # igualdad no es ascenso
    assert evaluate(legacy, bars, 3)[0] is False  # semántica histórica intacta
    definition = {"entry": corrected}
    assert supports_engine_version("lab-1.14", definition)
    assert not supports_engine_version("lab-1.13", definition)
    research = run_research(bars, corrected, "1h")
    assert [event["signal_time"] for event in research["events"]] == [START + 3 * STEP]


def test_research_api_persists_events_and_migrates_existing_v1_without_loss(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, _ = validate_candles(rows(), "1h", now=START + 20 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
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
        db.execute("DROP TABLE research_events")
        db.execute("DROP TABLE research_runs")
        db.execute("DROP TABLE optimization_runs")
        db.execute("DROP TABLE validation_runs")
        db.execute("ALTER TABLE datasets DROP COLUMN verified_public")
        db.execute("PRAGMA user_version=1")
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    response = client.post("/lab/research/runs", json={"dataset_id": dataset["id"], "rule": structure()})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["dataset_hash"] == dataset["hash"]
    assert saved["rule_hash"] == content_hash(saved["rule"])
    assert saved["result_hash"]
    assert saved["lab_engine_version"]
    assert saved["summary"]["cases"] == 3
    verified = client.get(f"/lab/research/runs/{saved['id']}/verify").json()
    assert verified["status"] == "verified" and all(verified["checks"].values())
    assert len(store.research_runs()) == 1
    assert store.dataset(dataset["id"])[0]["hash"] == dataset["hash"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert db.execute("SELECT count(*) FROM research_events").fetchone()[0] == 3
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    assert TestClient(reopened).get("/lab/research/runs/" + saved["id"]).json() == saved
    assert TestClient(reopened).get(f"/lab/research/runs/{saved['id']}/verify").json()["status"] == "verified"
    assert len(TestClient(reopened).get("/lab/research/runs").json()) == 1

    gap = store.save_dataset("BTC/USDT", "1h", "gap", [bars[0], *bars[2:]],
                             [{"after": bars[0].t, "before": bars[2].t, "missing": 1}])
    gap_response = client.post("/lab/research/runs", json={"dataset_id": gap["id"], "rule": structure()})
    assert gap_response.status_code == 200, gap_response.text
    gap_run = gap_response.json()
    assert gap_run["engine_version"] == "research-1.2"
    assert gap_run["summary"]["coverage"]["missing_bars"] == 1
    assert gap_run["summary"]["coverage"]["segments"] == 2
    assert client.get(f"/lab/research/runs/{gap_run['id']}/verify").json()["verified"] is True
    assert client.get(f"/lab/research/runs/{gap_run['id']}/export").status_code == 200
    assert client.post("/lab/research/runs", json={"dataset_id": dataset["id"],
                                                     "rule": {"kind": "FIBONACCI", "period": 3}}).status_code == 422
    assert client.get("/lab/research/runs", headers={"Origin": "https://external.invalid"}).status_code == 403


def test_research_verifies_existing_engine_1_0_without_rewriting_results(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, _ = validate_candles(rows(), "1h", now=START + 20 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    rule = validate_rule(structure())
    old_result = run_research(bars, rule, "1h", engine_version="research-1.0")
    saved = store.save_research_run(dataset["id"], dataset["hash"], old_result, "research-1.0",
                                    lab_engine_version="lab-1.13", expected_rule_hash=content_hash(rule))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    response = TestClient(app).get(f"/lab/research/runs/{saved['id']}/verify")
    assert response.status_code == 200
    assert response.json()["verified"] is True
    assert "coverage" not in store.research_run(saved["id"])["summary"]


def test_research_non_overlapping_horizons_reduce_clustered_cases_without_changing_raw_stats():
    raw = [{"t": START + i * STEP, "o": str(100 + i), "h": str(102 + i),
            "l": str(99 + i), "c": str(101 + i), "v": str(10 + i)} for i in range(30)]
    bars, _ = validate_candles(raw, "1h", now=START + 31 * STEP)
    rule = validate_rule({"kind": "VOLUME_RISING_STREAK", "period": 2})
    current = run_research(bars, rule, "1h")
    old = run_research(bars, rule, "1h", engine_version="research-1.1")
    assert current["events"] == old["events"]
    assert current["summary"]["horizons"]["14400"]["count"] == 25
    assert current["summary"]["horizons"]["14400"]["non_overlapping"]["count"] == 7
    assert current["summary"]["horizons"]["3600"]["non_overlapping"]["count"] == 28
    assert "non_overlapping" not in old["summary"]["horizons"]["14400"]
    for horizon in ("3600", "14400", "86400", "604800"):
        assert {key: value for key, value in current["summary"]["horizons"][horizon].items()
                if key != "non_overlapping"} == old["summary"]["horizons"][horizon]
    assert current["summary"]["horizons"]["604800"]["non_overlapping"]["count"] == 0


def test_research_verifies_existing_engine_1_1_without_rewriting_results(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, _ = validate_candles(rows(), "1h", now=START + 20 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    rule = validate_rule(structure())
    old_result = run_research(bars, rule, "1h", engine_version="research-1.1")
    saved = store.save_research_run(dataset["id"], dataset["hash"], old_result, "research-1.1",
                                    lab_engine_version="lab-1.13", expected_rule_hash=content_hash(rule))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    response = TestClient(app).get(f"/lab/research/runs/{saved['id']}/verify")
    assert response.status_code == 200
    assert response.json()["verified"] is True
    assert "non_overlapping" not in store.research_run(saved["id"])["summary"]["horizons"]["3600"]


def test_research_verifier_detects_rule_event_dataset_and_legacy(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, _ = validate_candles(rows(), "1h", now=START + 20 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)

    def create_run():
        response = client.post("/lab/research/runs", json={"dataset_id": dataset["id"], "rule": structure()})
        assert response.status_code == 200, response.text
        return response.json()["id"]

    run_id = create_run()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE research_events SET event_json=? WHERE run_id=? AND index_no=0",
                   ('{"signal_time":0}', run_id))
    check = client.get(f"/lab/research/runs/{run_id}/verify").json()
    assert check["status"] == "mismatch" and check["checks"]["stored_result"] is False

    run_id = create_run()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE research_runs SET rule_json=? WHERE id=?",
                   ('{"kind":"BEARISH_STREAK","period":2}', run_id))
    check = client.get(f"/lab/research/runs/{run_id}/verify").json()
    assert check["status"] == "mismatch" and check["checks"]["rule"] is False

    run_id = create_run()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE research_runs SET engine_version=? WHERE id=?", ("research-obsolete", run_id))
    assert client.get(f"/lab/research/runs/{run_id}/verify").json()["status"] == "engine_unavailable"

    run_id = create_run()
    with sqlite3.connect(path) as db:
        result = db.execute("SELECT result_json FROM research_runs WHERE id=?", (run_id,)).fetchone()[0]
        import json
        legacy = json.loads(result)
        for field in ("rule_hash", "result_hash", "lab_engine_version"):
            legacy.pop(field)
        db.execute("UPDATE research_runs SET result_json=? WHERE id=?", (json.dumps(legacy), run_id))
    assert client.get(f"/lab/research/runs/{run_id}/verify").json()["status"] == "legacy_unpinned"

    run_id = create_run()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE datasets SET candles_json=? WHERE id=?", ('[]', dataset["id"]))
    check = client.get(f"/lab/research/runs/{run_id}/verify").json()
    assert check["status"] == "mismatch" and check["checks"]["dataset"] is None


def test_research_export_separates_features_and_future_and_fails_closed(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    raw = [{"t": START + i * STEP, "o": str(1000 - i),
            "h": str(1001 - i), "l": str(998 - i),
            "c": str(999 - i), "v": str(10 + i)} for i in range(180)]
    bars, _ = validate_candles(raw, "1h", now=START + 181 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    created = client.post("/lab/research/runs", json={
        "dataset_id": dataset["id"], "rule": {"kind": "BEARISH_STREAK", "period": 3}})
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    response = client.get(f"/lab/research/runs/{run_id}/export")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    bundle = response.json()
    assert bundle["schema"] == "orbis.lab.research.evidence.v2"
    assert bundle["environment"] == "LAB" and bundle["theoretical_only"] is True
    assert bundle["dataset"]["hash"] == dataset["hash"]
    assert bundle["rule"]["hash"] == content_hash(created.json()["rule"])
    assert bundle["verification"]["verified"] is True
    assert len(bundle["candles"]) == 180
    first, last = bundle["cases"][0], bundle["cases"][-1]
    assert first["features_at_close"]["signal_time"] == START + 2 * STEP
    assert first["features_at_close"]["candle"] == raw[2]
    assert "future_labels" not in first["features_at_close"]
    assert first["future_labels"]["full_7d_followup"] is True
    assert first["future_labels"]["max_down_pct"] is not None
    assert first["future_labels"]["time_to_max_seconds"] == (
        first["future_labels"]["max_at"] - first["features_at_close"]["signal_time"])
    assert first["future_labels"]["time_to_min_seconds"] == (
        first["future_labels"]["min_at"] - first["features_at_close"]["signal_time"])
    assert last["future_labels"]["full_7d_followup"] is False
    assert last["future_labels"]["max_down_pct"] is None
    assert last["future_labels"]["time_to_max_seconds"] is None
    assert last["future_labels"]["time_to_min_seconds"] is None
    assert last["future_labels"]["outcomes_pct"]["604800"] is None
    legacy = client.get(f"/lab/research/runs/{run_id}/export?schema_version=v1")
    assert legacy.status_code == 200
    assert legacy.json()["schema"] == "orbis.lab.research.evidence.v1"
    assert "time_to_max_seconds" not in legacy.json()["cases"][0]["future_labels"]
    assert legacy.json()["run"]["result_hash"] == bundle["run"]["result_hash"]
    split_export = client.get(f"/lab/research/runs/{run_id}/export?schema_version=v3")
    assert split_export.status_code == 200, split_export.text
    split_bundle = split_export.json()
    assert split_bundle["schema"] == "orbis.lab.research.evidence.v3"
    assert split_bundle["candles"] == bundle["candles"]
    assert sum(split_bundle["split"]["counts"].values()) == len(split_bundle["cases"])
    assert all("partition" in item for item in split_bundle["cases"])
    for phase in ("development", "validation"):
        assert set(split_bundle["split"][f"{phase}_nonoverlap_indices"]).issubset(
            split_bundle["split"][f"{phase}_indices"])
    assert "partition" not in first
    assert client.get(f"/lab/research/runs/{run_id}/export?schema_version=v4").status_code == 422
    assert client.get("/lab/research/runs/unknown/export").status_code == 404
    assert client.get(f"/lab/research/runs/{run_id}/export",
                      headers={"Origin": "https://external.invalid"}).status_code == 403

    with sqlite3.connect(path) as db:
        db.execute("UPDATE research_events SET event_json=? WHERE run_id=? AND index_no=0",
                   ('{"signal_time":0}', run_id))
    assert client.get(f"/lab/research/runs/{run_id}/export").status_code == 409


def test_research_save_rejects_dataset_or_rule_changed_after_calculation(tmp_path):
    store = LabStore(tmp_path / "lab.db")
    bars, _ = validate_candles(rows(), "1h", now=START + 20 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    rule = validate_rule(structure())
    result = run_research(bars, rule, "1h")
    with pytest.raises(LabError, match="regla cambió"):
        store.save_research_run(dataset["id"], dataset["hash"], result, "research-1.0",
                                expected_rule_hash=content_hash({"kind": "BEARISH_STREAK", "period": 2}))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE datasets SET candles_json=? WHERE id=?", ('[]', dataset["id"]))
    with pytest.raises(LabError, match="Huella del dataset cambió"):
        store.save_research_run(dataset["id"], dataset["hash"], result, "research-1.0",
                                expected_rule_hash=content_hash(rule))
    assert store.research_runs() == []
