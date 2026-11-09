"""Actualizar la semilla PAPER nunca modifica el histórico de la prueba LAB."""

from app.lab.schema import CURRENT_SCHEMA_VERSION
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.lab.api as lab_api
from app.lab.engine import INTERVALS, validate_candles
from app.lab.backup import create_lab_backup, verify_lab_backup
from app.lab.engine import LabError
from app.lab.store import LabStore


NOW = int(datetime(2025, 3, 1, 12, 30, tzinfo=timezone.utc).timestamp())


class FixedDateTime:
    @staticmethod
    def now(_zone):
        return datetime.fromtimestamp(NOW, timezone.utc)


def rows(start, count, step):
    return [{"t": start + index * step, "o": "100", "h": "101", "l": "99",
             "c": "100", "v": "10"} for index in range(count)]


def test_mexc_pagination_bounds_both_ends_and_respects_observed_500_cap():
    step = INTERVALS["1h"]
    end = NOW // step * step
    start = end - 1000 * step
    calls = []

    class CappedSource(lab_api.MexcAdapter):
        def get_candles(self, symbol, timeframe="1h", limit=200, since=None, before=None):
            calls.append((since, before, limit))
            assert symbol == "BTC/USDT" and before == end
            return rows(since, min(limit, 500), step)

    result = lab_api.download_binance("BTC/USDT", "1h", start, end,
                                      adapter=CappedSource(), now=NOW)
    assert len(result) == 1000
    assert result[0]["t"] == start and result[-1]["t"] == end - step
    assert calls == [(start, end, 500), (start + 500 * step, end, 500)]


@pytest.mark.parametrize("source,symbol,timeframe,adapter_name", [
    ("mexc-spot-public", "BTC/USDT", "1h", "MexcAdapter"),
    ("grvt-perp-trade-public", "BTC/USDT:PERP", "1h", "GrvtAdapter"),
])
def test_refresh_creates_new_verified_seed_and_preserves_old_dataset(
        tmp_path, monkeypatch, source, symbol, timeframe, adapter_name):
    step = INTERVALS[timeframe]
    class Source:
        known_pairs = (symbol,)

        def get_candles(self, pair, timeframe="1h", limit=1000, since=None, before=None):
            assert pair == symbol and before == NOW // step * step
            return rows(since, limit, step)

        def get_historical_candles(self, pair, interval, start, end, *, now):
            assert pair == symbol and interval == timeframe and now == NOW
            return rows(start, (end - start) // step, step)

    monkeypatch.setattr(lab_api, "datetime", FixedDateTime)
    monkeypatch.setattr(lab_api, adapter_name, Source)
    lab = LabStore(tmp_path / "lab.db")
    original_rows = rows((NOW // step - 2008) * step, 8, step)
    bars, gaps = validate_candles(original_rows, timeframe, now=NOW)
    original = lab.save_dataset(symbol, timeframe, source, bars, gaps, verified_public=True)
    app = FastAPI()
    app.include_router(lab_api.create_lab_router(lab.path))
    client = TestClient(app)
    strategy = lab.create_strategy("Prueba fijada", {
        "direction": "LONG", "initial_capital": "1000", "allocation": "1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
        "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}})
    run = client.post("/lab/runs", json={"dataset_id": original["id"],
                                         "version_id": strategy["version_id"]})
    assert run.status_code == 200, run.text
    run_id = run.json()["id"]
    assert client.get(f"/lab/runs/{run_id}/verify").json()["verified"] is True
    response = client.post("/lab/datasets/refresh-for-paper", json={
        "source_dataset_id": original["id"]})
    assert response.status_code == 200, response.text
    fresh = response.json()
    assert fresh["id"] != original["id"]
    assert fresh["source"] == source and fresh["symbol"] == symbol
    assert fresh["verified_public"] is True and fresh["count"] == 1000
    assert fresh["end_t"] == NOW // step * step - step
    assert lab.dataset(original["id"])[0] == original
    assert lab.dataset(original["id"])[1] == original_rows
    assert client.get(f"/lab/runs/{run_id}/verify").json()["verified"] is True
    lineage = client.get(f"/lab/datasets/{fresh['id']}/lineage")
    assert lineage.status_code == 200
    assert [(row["parent_id"], row["parent_hash"], row["child_hash"])
            for row in lineage.json()] == [(original["id"], original["hash"], fresh["hash"])]
    assert LabStore(lab.path).dataset_lineage(fresh["id"]) == lineage.json()
    assert len(lab.datasets()) == 2
    again = client.post("/lab/datasets/refresh-for-paper", json={
        "source_dataset_id": original["id"]})
    assert again.status_code == 200 and again.json()["id"] == fresh["id"]
    assert len(lab.dataset_lineage(fresh["id"])) == 1


def test_refresh_rejects_incomplete_provider_without_saving_partial_data(tmp_path, monkeypatch):
    step = INTERVALS["1h"]
    class ShortSource:
        known_pairs = ("BTC/USDT",)

        def get_candles(self, pair, timeframe="1h", limit=1000, since=None, before=None):
            return rows(since, limit - 1, step)

    monkeypatch.setattr(lab_api, "datetime", FixedDateTime)
    monkeypatch.setattr(lab_api, "MexcAdapter", ShortSource)
    lab = LabStore(tmp_path / "lab.db")
    bars, gaps = validate_candles(rows((NOW // step - 2008) * step, 8, step), "1h", now=NOW)
    original = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars, gaps,
                                verified_public=True)
    app = FastAPI()
    app.include_router(lab_api.create_lab_router(lab.path))
    result = TestClient(app).post("/lab/datasets/refresh-for-paper", json={
        "source_dataset_id": original["id"]})
    assert result.status_code == 422
    assert len(lab.datasets()) == 1


def test_v6_lab_migrates_lineage_table_without_changing_existing_dataset(tmp_path):
    path = tmp_path / "lab.db"
    lab = LabStore(path)
    step = INTERVALS["1h"]
    bars, gaps = validate_candles(rows((NOW // step - 8) * step, 8, step), "1h", now=NOW)
    original = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars, gaps,
                                verified_public=True)
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
        db.execute("PRAGMA user_version=6")
    assert verify_lab_backup(path)["schema_version"] == 6
    reopened = LabStore(path)
    assert reopened.dataset(original["id"])[0] == original
    assert reopened.dataset_lineage(original["id"]) == []
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_v7_lab_migrates_gap_archive_without_changing_lineage(tmp_path):
    path = tmp_path / "lab.db"
    lab = LabStore(path)
    step = INTERVALS["1h"]
    old, gaps = validate_candles(rows((NOW // step - 8) * step, 8, step), "1h", now=NOW)
    parent = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", old, gaps,
                              verified_public=True)
    changed = [dict(row) for row in rows((NOW // step - 8) * step, 8, step)]
    changed[0]["v"] = "99"
    fresh, _ = validate_candles(changed, "1h", now=NOW)
    child = lab.save_refreshed_dataset(parent["id"], parent["hash"],
                                       "BTC/USDT", "1h", "mexc-spot-public", fresh)
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
        db.execute("PRAGMA user_version=7")
    assert verify_lab_backup(path)["schema_version"] == 7
    reopened = LabStore(path)
    assert reopened.dataset_lineage(child["id"])[0]["parent_id"] == parent["id"]
    assert reopened.paper_gap_reports("missing") == []
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_backup_rejects_tampered_dataset_lineage(tmp_path):
    lab = LabStore(tmp_path / "lab.db")
    step = INTERVALS["1h"]
    old, gaps = validate_candles(rows((NOW // step - 2008) * step, 8, step), "1h", now=NOW)
    parent = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", old, gaps,
                              verified_public=True)
    fresh, _ = validate_candles(rows((NOW // step - 1000) * step, 1000, step), "1h", now=NOW)
    child = lab.save_refreshed_dataset(parent["id"], parent["hash"],
                                       "BTC/USDT", "1h", "mexc-spot-public", fresh)
    backup = create_lab_backup(lab)
    assert backup["verified"]["counts"]["dataset_refreshes"] == 1
    with sqlite3.connect(backup["path"]) as db:
        db.execute("UPDATE dataset_refreshes SET parent_hash=? WHERE child_id=?",
                   ("0" * 64, child["id"]))
    with pytest.raises(LabError, match="procedencia"):
        verify_lab_backup(Path(backup["path"]))


def test_refresh_rejects_cycle_when_provider_reverts_to_older_content(tmp_path):
    lab = LabStore(tmp_path / "lab.db")
    step = INTERVALS["1h"]
    original_rows = rows((NOW // step - 8) * step, 8, step)
    old, gaps = validate_candles(original_rows, "1h", now=NOW)
    parent = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", old, gaps,
                              verified_public=True)
    changed = [dict(row) for row in original_rows]
    changed[0]["v"] = "99"
    new, _ = validate_candles(changed, "1h", now=NOW)
    child = lab.save_refreshed_dataset(parent["id"], parent["hash"],
                                       "BTC/USDT", "1h", "mexc-spot-public", new)
    with pytest.raises(LabError, match="ciclo"):
        lab.save_refreshed_dataset(child["id"], child["hash"],
                                   "BTC/USDT", "1h", "mexc-spot-public", old)
    assert lab.dataset_lineage(parent["id"]) == []
    assert len(lab.dataset_lineage(child["id"])) == 1


def test_refresh_rejects_parent_with_modified_candles_inside_transaction(tmp_path):
    lab = LabStore(tmp_path / "lab.db")
    step = INTERVALS["1h"]
    original_rows = rows((NOW // step - 8) * step, 8, step)
    old, gaps = validate_candles(original_rows, "1h", now=NOW)
    parent = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", old, gaps,
                              verified_public=True)
    with sqlite3.connect(lab.path) as db:
        altered = [dict(row) for row in original_rows]
        altered[0]["v"] = "99"
        db.execute("UPDATE datasets SET candles_json=? WHERE id=?",
                   (json.dumps(altered), parent["id"]))
    with pytest.raises(LabError, match="huella"):
        lab.save_refreshed_dataset(parent["id"], parent["hash"],
                                   "BTC/USDT", "1h", "mexc-spot-public", old)
    assert len(lab.datasets()) == 1


def test_refresh_store_rejects_discontinuous_child_without_archiving_lineage(tmp_path):
    lab = LabStore(tmp_path / "lab.db")
    step = INTERVALS["1h"]
    old, gaps = validate_candles(rows((NOW // step - 8) * step, 8, step), "1h", now=NOW)
    parent = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", old, gaps,
                              verified_public=True)
    future = [old[0], old[1], old[3], old[4]]

    with pytest.raises(LabError, match="huecos"):
        lab.save_refreshed_dataset(parent["id"], parent["hash"],
                                   "BTC/USDT", "1h", "mexc-spot-public", future)

    assert lab.datasets() == [parent]
    assert lab.dataset_lineage(parent["id"]) == []


def test_dataset_store_rejects_gap_metadata_that_disagrees_with_candles(tmp_path):
    lab = LabStore(tmp_path / "lab.db")
    step = INTERVALS["1h"]
    old, _ = validate_candles(rows((NOW // step - 8) * step, 8, step), "1h", now=NOW)
    broken = [old[0], old[1], old[3], old[4]]

    with pytest.raises(LabError, match="cobertura"):
        lab.save_dataset("BTC/USDT", "1h", "fixture", broken, [])

    assert lab.datasets() == []


def test_dataset_read_and_backup_reject_altered_coverage_metadata(tmp_path):
    lab = LabStore(tmp_path / "lab.db")
    step = INTERVALS["1h"]
    old, gaps = validate_candles(rows((NOW // step - 8) * step, 8, step), "1h", now=NOW)
    dataset = lab.save_dataset("BTC/USDT", "1h", "fixture", old, gaps)
    with sqlite3.connect(lab.path) as db:
        db.execute("UPDATE datasets SET gaps_json=? WHERE id=?",
                   (json.dumps([{"after": old[0].t, "before": old[2].t, "missing": 1}]),
                    dataset["id"]))

    with pytest.raises(LabError, match="cobertura"):
        lab.dataset(dataset["id"])
    with pytest.raises(LabError, match="cobertura"):
        create_lab_backup(lab)


def test_backup_rejects_cyclic_lineage_even_with_matching_hashes(tmp_path):
    lab = LabStore(tmp_path / "lab.db")
    step = INTERVALS["1h"]
    original_rows = rows((NOW // step - 8) * step, 8, step)
    old, gaps = validate_candles(original_rows, "1h", now=NOW)
    parent = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", old, gaps,
                              verified_public=True)
    changed = [dict(row) for row in original_rows]
    changed[0]["v"] = "99"
    new, _ = validate_candles(changed, "1h", now=NOW)
    child = lab.save_refreshed_dataset(parent["id"], parent["hash"],
                                       "BTC/USDT", "1h", "mexc-spot-public", new)
    backup = create_lab_backup(lab)
    with sqlite3.connect(backup["path"]) as db:
        db.execute("INSERT INTO dataset_refreshes VALUES (?,?,?,?,?)",
                   (child["id"], parent["id"], child["hash"], parent["hash"],
                    "2025-03-01T00:00:00+00:00"))
    with pytest.raises(LabError, match="ciclo"):
        verify_lab_backup(Path(backup["path"]))
