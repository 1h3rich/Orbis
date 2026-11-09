"""Estrategia LAB LONG seguida sobre MEXC Spot con capital ficticio aislado."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import sqlcipher3

from app.automation.spot_strategy_paper import (SpotPaperLocked, SpotStrategyError, SpotStrategyPaper, SpotStrategyUnavailable,
                                                 create_spot_strategy_router)
from app.automation.spot_paper_access import (SpotPaperAccess, SpotPaperAccessError,
                                              spot_paper_access, spot_paper_format)
from app.automation.spot_paper_lease import spot_paper_lease
from app.automation.spot_paper_migration import (encrypt_active_spot_paper,
                                                  restore_secure_spot_paper,
                                                  verify_spot_paper_database)
from app.automation.spot_strategy_worker import SpotStrategyWorker
from app.lab.engine import CURRENT_ENGINE_VERSION, IndicatorCache, LabError, evaluate, run_backtest, validate_candles, validate_strategy
from app.lab.backup import create_lab_backup, verify_lab_backup
from app.lab.api import create_lab_router
from app.lab.store import LabStore
from app.lab.paper_indicator_state import paper_indicator_state_matches
from app.live_execution.journal import (LiveJournalError, recover_spot_signal_source,
                                        register_spot_policy, reserve_spot_intent)
from app.live_execution.mexc_instrument import MexcSpotRules
from app.live_execution.mexc_live_order import MexcOrderAcknowledgement, MexcOrderSendError
from app.live_execution.mexc_live_submit import MexcLiveInputs, submit_spot_limit_once
from app.live_execution.reconciliation_view import list_spot_execution_intents
from app.live_execution.spot_paper_signal import (
    SpotPaperSignalError, current_spot_paper_entry_signal,
    live_buy_intent_from_signal, reserve_current_paper_buy,
    verified_archived_paper_buy_source,
)
from app.live_execution.policy import LiveSpotPolicy, LiveSpotState
from app.marketdata.adapters.mexc import MexcAdapter
from app.portfolio.vault import RealVault


STEP = 3600
START = 1_735_689_600


def test_spot_chart_loader_collects_pages_and_rejects_changed_snapshot():
    if not shutil.which("node"):
        pytest.skip("node no disponible")
    source = (Path(__file__).resolve().parent.parent / "app/web/static/spot-paper.js").read_text()
    function = "function fetchSpotChartPages(" + source.split(
        "  function fetchSpotChartPages(", 1)[1].split("\n  function loadChart(", 1)[0]
    script = function + """
const snapshot = {last_t:3000,event_count:2,last_event_ns:9};
const paths = [];
const item = {id:'s',symbol:'BTC/USDT',timeframe:'1h',last_t:3000,event_count:2};
function request(path) {
  paths.push(path);
  const base = {session_id:'s',symbol:'BTC/USDT',timeframe:'1h',snapshot};
  if (path.includes('/candles')) return Promise.resolve(Object.assign(base,
    path.includes('after_t=2000') ?
      {candles:[{candle:{t:3000}}],has_more:false,next_after_t:null} :
      {candles:[{candle:{t:1000}},{candle:{t:2000}}],has_more:true,next_after_t:2000}));
  return Promise.resolve(Object.assign(base, {execution:'PAPER_ACTION_AFTER_SIGNAL_CANDLE_CLOSE_NO_REAL_ORDER',
    initial_last_t:2000}, path.includes('after_t=2000') ?
      {markers:[{time:3000,kind:'SELL'}],observations:[],has_more:false} :
      {markers:[{time:2000,kind:'START'},{time:2000,kind:'SIGNAL'}],
       observations:[{candle_t:2000,kind:'OBSERVATION'}],has_more:true,
       next_after_t:2000,next_after_kind:'OBSERVATION'}));
}
fetchSpotChartPages(request,()=>true,item).then(result=>{
  console.log(JSON.stringify({result,paths}));
  return fetchSpotChartPages(path=>request(path).then(page=>{
    if (path.includes('/candles?limit=1000&after_t=2000')) page.snapshot = {...snapshot,event_count:3};
    return page;
  }),()=>true,item).then(()=>console.log('MISMATCH_ACCEPTED'),
     error=>console.log('MISMATCH_REJECTED:'+error.message));
});
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    lines = proc.stdout.splitlines()
    data = json.loads(lines[0])
    assert [bar["t"] for bar in data["result"]["candles"]] == [1000, 2000, 3000]
    assert [mark["kind"] for mark in data["result"]["markers"]] == ["START", "SIGNAL", "SELL"]
    assert len(data["result"]["observations"]) == 1
    assert len(data["paths"]) == 4
    assert lines[1].startswith("MISMATCH_REJECTED:")


def test_new_spot_paper_is_not_created_by_worker_and_requires_encrypted_setup(tmp_path):
    path = tmp_path / "spot-paper.db"
    paper = SpotStrategyPaper(path, tmp_path / "lab.db", allow_create=False)
    assert paper.sessions() == []
    assert SpotStrategyWorker(paper).run_once() == {}
    assert not path.exists()
    app = FastAPI()
    app.include_router(create_spot_strategy_router(path, tmp_path / "lab.db"))
    client = TestClient(app)
    assert client.get("/paper/spot/strategies").json() == []
    assert client.post("/paper/spot/strategies", json={
        "version_id": "missing", "dataset_id": "missing"}).status_code == 423
    assert not path.exists()
    password = "new encrypted spot paper password"
    assert client.post("/paper/spot/strategies/create", json={"passphrase": password},
                       headers={"Origin": "https://outside.invalid"}).status_code == 403
    assert not path.exists()
    created = client.post("/paper/spot/strategies/create", json={"passphrase": password})
    assert created.status_code == 200 and created.json()["unlocked"] is True
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert client.post("/paper/spot/strategies/create", json={"passphrase": password}).status_code == 422
    assert client.post("/paper/spot/strategies/lock").json()["unlocked"] is False
    assert client.get("/paper/spot/strategies").status_code == 423
    assert client.post("/paper/spot/strategies/unlock", json={"passphrase": password}).status_code == 200


def test_new_encrypted_spot_paper_persists_session_after_lock_and_unlock(tmp_path):
    paper, _, _, dataset, strategy = harness(tmp_path)
    password = "spot paper first session password"
    spot_paper_access.create(paper.path, password)
    session = paper.create(strategy["version_id"], dataset["id"])
    assert paper.detail(session["id"])["status"] == "ACTIVE"
    assert paper.path.read_bytes()[:16] != b"SQLite format 3\x00"
    spot_paper_access.lock(paper.path)
    with pytest.raises(SpotPaperLocked):
        paper.detail(session["id"])
    spot_paper_access.unlock(paper.path, password)
    reopened = SpotStrategyPaper(paper.path, paper.lab.path, allow_create=False)
    assert reopened.detail(session["id"])["id"] == session["id"]


def test_spot_paper_detail_exposes_remaining_candle_capacity(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    assert paper.detail(session["id"])["capacity_remaining"] == 4997
    state["now"] = START + 4 * STEP + 1
    assert paper.poll(session["id"])["capacity_remaining"] == 4996


def test_spot_paper_v1_migration_preserves_session_without_source_run(tmp_path):
    paper, _, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with sqlite3.connect(paper.path) as db:
        db.execute("DROP TABLE session_candles")
        db.execute("ALTER TABLE sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE observed_candles")
        db.execute("ALTER TABLE sessions DROP COLUMN source_run_id")
        db.execute("PRAGMA user_version=1")
    reopened = SpotStrategyPaper(paper.path, paper.lab.path)
    assert reopened.detail(session["id"])["source_run_id"] is None
    with sqlite3.connect(paper.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_spot_paper_archives_observed_candle_and_backfills_v2(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    observed = paper.poll(session["id"])
    candle = observed["events"][0]["candle"]
    with paper.connect() as db:
        row = db.execute("SELECT candle_t,candle_json FROM observed_candles WHERE session_id=?",
                         (session["id"],)).fetchone()
        assert row["candle_t"] == candle["t"]
        assert json.loads(row["candle_json"]) == candle

    with sqlite3.connect(paper.path) as db:
        db.execute("DROP TABLE session_candles")
        db.execute("ALTER TABLE sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE observed_candles")
        db.execute("PRAGMA user_version=2")
    reopened = SpotStrategyPaper(paper.path, paper.lab.path)
    assert reopened.detail(session["id"])["events"] == observed["events"]
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        row = db.execute("SELECT candle_json FROM observed_candles WHERE session_id=?",
                         (session["id"],)).fetchone()
        assert json.loads(row["candle_json"]) == candle


def test_spot_paper_full_archive_pages_seed_and_observation(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    first = paper.candle_page(session["id"], limit=2)
    assert [item["origin"] for item in first["candles"]] == ["SEED", "SEED"]
    assert first["has_more"] and first["next_after_t"] == START + STEP
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    second = paper.candle_page(session["id"], first["next_after_t"], limit=2)
    assert [item["origin"] for item in second["candles"]] == ["SEED", "OBSERVED"]
    assert not second["has_more"] and second["next_after_t"] is None
    assert paper.candle_page(session["id"], second["candles"][-1]["candle"]["t"])["candles"] == []
    assert verify_spot_paper_database(paper.path)["counts"]["session_candles"] == 4


def test_spot_paper_candle_pages_api_is_bounded_and_local(tmp_path):
    paper, _, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    client = TestClient(app)
    response = client.get(f"/paper/spot/strategies/{session['id']}/candles?limit=2")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert len(response.json()["candles"]) == 2 and response.json()["has_more"]
    assert client.get(f"/paper/spot/strategies/{session['id']}/candles?limit=1001").status_code == 422
    assert client.get(f"/paper/spot/strategies/{session['id']}/candles",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_spot_paper_candle_page_rejects_changed_segment_or_anchor(tmp_path):
    paper, _, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("""UPDATE session_candles SET candle_json=json_set(candle_json,'$.v','999')
                      WHERE session_id=? AND candle_t=?""", (session["id"], START + STEP))
    with pytest.raises(SpotStrategyError, match="Huella de vela PAPER divergente"):
        paper.candle_page(session["id"], limit=2)
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    assert TestClient(app).get(f"/paper/spot/strategies/{session['id']}/candles?limit=2").status_code == 422

    with paper.connect() as db:
        original = db.execute("SELECT history_json FROM sessions WHERE id=?", (session["id"],)).fetchone()[0]
        candle = json.loads(original)[1]
        db.execute("UPDATE session_candles SET candle_json=? WHERE session_id=? AND candle_t=?",
                   (json.dumps(candle, sort_keys=True, separators=(",", ":")), session["id"], START + STEP))
        db.execute("UPDATE session_archive_chain SET digest=? WHERE session_id=? AND candle_t=?",
                   ("0" * 64, session["id"], START + STEP))
    with pytest.raises(SpotStrategyError, match="Huella de vela PAPER divergente"):
        paper.candle_page(session["id"], after_t=START + STEP, limit=1)


def test_spot_paper_event_pages_keep_same_candle_events_in_order(tmp_path):
    paper, _, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", [
            (session["id"], START, "A", 1, '{"marker":"a"}'),
            (session["id"], START, "B", 2, '{"marker":"b"}'),
            (session["id"], START + STEP, "A", 3, '{"marker":"c"}')])
    first = paper.event_page(session["id"], limit=1)
    assert [event["marker"] for event in first["events"]] == ["a"]
    second = paper.event_page(session["id"], first["next_after_t"], first["next_after_kind"], 1)
    third = paper.event_page(session["id"], second["next_after_t"], second["next_after_kind"], 1)
    assert [event["marker"] for event in second["events"] + third["events"]] == ["b", "c"]
    assert not third["has_more"]
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    client = TestClient(app)
    response = client.get(f"/paper/spot/strategies/{session['id']}/events?limit=1")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["events"] == first["events"]
    assert client.get(f"/paper/spot/strategies/{session['id']}/events?limit=1001").status_code == 422
    assert client.get(f"/paper/spot/strategies/{session['id']}/events",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_spot_paper_recent_events_page_backwards_and_summary_omits_diary(tmp_path):
    paper, _, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", [
            (session["id"], START, "A", 1, '{"marker":"a"}'),
            (session["id"], START, "B", 2, '{"marker":"b"}'),
            (session["id"], START + STEP, "A", 3, '{"marker":"c"}')])
    latest = paper.recent_event_page(session["id"], limit=1)
    middle = paper.recent_event_page(session["id"], latest["next_before_t"],
                                     latest["next_before_kind"], 1)
    oldest = paper.recent_event_page(session["id"], middle["next_before_t"],
                                     middle["next_before_kind"], 1)
    assert [page["events"][0]["marker"] for page in (latest, middle, oldest)] == ["c", "b", "a"]
    assert not oldest["has_more"] and oldest["next_before_t"] is None
    detail = paper.detail(session["id"])
    summary = paper.summary(session["id"])
    assert "events" not in summary
    assert summary["event_count"] == len(detail["events"]) == 3
    assert summary["state"] == detail["state"]
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    client = TestClient(app)
    prefix = f"/paper/spot/strategies/{session['id']}"
    response = client.get(prefix + "/summary")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "events" not in response.json() and response.json()["event_count"] == 3
    response = client.get(prefix + "/events/recent?limit=1")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["events"] == latest["events"]
    assert client.get(prefix + "/events/recent?limit=101").status_code == 422
    assert client.get(prefix + "/events/recent", headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_spot_paper_chart_marker_pages_match_full_chart_with_manual_exit(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    bucket = START + 4 * STEP
    paper.clock_ns = lambda: state["now"] * 1_000_000_000 + 123_456_789
    state["now"] = bucket + 1
    state["quote_event_ns"] = bucket * 1_000_000_000 + 900_000_001
    buy = paper.poll(session["id"])["events"][0]
    assert buy["action"]["side"] == "BUY"
    assert buy["observed_at_ns_exact"] == str((bucket + 1) * 1_000_000_000 + 123_456_789)
    state["now"] = bucket + 2
    assert paper.close_position(session["id"])["close_result"] == "CLOSED"
    full = paper.chart(session["id"])
    pages = []
    cursor_t, cursor_kind = None, ""
    while True:
        page = paper.chart_marker_page(session["id"], cursor_t, cursor_kind, 1)
        pages.append(page)
        if not page["has_more"]:
            break
        cursor_t, cursor_kind = page["next_after_t"], page["next_after_kind"]
    markers = [marker for page in pages for marker in page["markers"]]
    assert sorted(markers, key=lambda row: (row["time"], row["kind"])) == sorted(
        full["markers"], key=lambda row: (row["time"], row["kind"]))
    assert [event["kind"] for page in pages for event in page["observations"]] == ["OBSERVATION"]
    entry, manual = (mark for mark in markers if mark["kind"] in {"BUY", "SELL"})
    assert entry["kind"] == "BUY" and entry["source_candle_t"] == bucket - STEP
    assert entry["quote_bid"] == "99" and entry["quote_ask"] == "100"
    assert entry["price"] == buy["action"]["price"]
    assert entry["quote_time_ns_exact"] == str(state["quote_event_ns"])
    assert entry["observed_at_ns_exact"] == buy["observed_at_ns_exact"]
    assert entry["time"] == manual["time"] == bucket
    assert entry["execution_time"] == bucket + 1 and manual["execution_time"] == bucket + 2
    assert manual["kind"] == "SELL" and manual["source_candle_t"] is None
    assert entry["pending_candle"] and manual["pending_candle"]
    reopened = SpotStrategyPaper(paper.path, paper.lab.path, allow_create=False)
    assert reopened.chart(session["id"]) == full
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    client = TestClient(app)
    response = client.get(f"/paper/spot/strategies/{session['id']}/chart-markers-page?limit=1")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["markers"] == pages[0]["markers"]
    assert client.get(f"/paper/spot/strategies/{session['id']}/chart-markers-page?limit=1001").status_code == 422
    assert client.get(f"/paper/spot/strategies/{session['id']}/chart-markers-page",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_spot_paper_v4_backfills_full_archive_without_losing_session(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    with sqlite3.connect(paper.path) as db:
        db.execute("DROP TABLE session_candles")
        db.execute("PRAGMA user_version=4")
    reopened = SpotStrategyPaper(paper.path, paper.lab.path)
    assert [item["origin"] for item in reopened.candle_page(session["id"])["candles"]] == [
        "SEED", "SEED", "SEED", "OBSERVED"]
    assert verify_spot_paper_database(paper.path)["schema_version"] == 6


def test_spot_paper_v5_migrates_archive_chain_and_preserves_observation(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    with sqlite3.connect(paper.path) as db:
        db.execute("DROP TABLE session_archive_chain")
        db.execute("PRAGMA user_version=5")
    reopened = SpotStrategyPaper(paper.path, paper.lab.path)
    assert reopened.detail(session["id"])["status"] == "ACTIVE"
    report = verify_spot_paper_database(paper.path)
    assert report["schema_version"] == 6
    assert report["counts"]["session_archive_chain"] == 4


def test_spot_paper_chain_detects_changed_digest_before_acting(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("""UPDATE session_archive_chain SET digest=?
                      WHERE session_id=? AND candle_t=?""", ("0" * 64, session["id"], START + STEP))
    with pytest.raises(SpotPaperAccessError, match="cadena de velas divergente"):
        verify_spot_paper_database(paper.path)
    state["now"] = START + 4 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == "GAP" and result["events"] == []


def test_spot_paper_rejects_divergent_full_archive_before_acting(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("""UPDATE session_candles SET candle_json=json_set(candle_json,'$.c','999')
                      WHERE session_id=? AND origin='SEED' AND candle_t=?""",
                   (session["id"], START))
    with pytest.raises(SpotPaperAccessError, match="archivo completo divergente"):
        verify_spot_paper_database(paper.path)
    state["now"] = START + 4 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == "GAP" and result["events"] == []


def test_spot_paper_v4_persists_indicator_state_and_backfills_v3(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    rules = {**definition(), "entry": {"op": "AND", "children": [
        {"kind": "CLOSE_ABOVE_EMA", "period": 2},
        {"kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 2, "slow_period": 3, "signal_period": 2},
        {"kind": "ATR_PCT_GT", "period": 2, "threshold": "0.1"}]}}
    strategy = lab.create_strategy("Indicadores PAPER", rules)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    with paper.connect() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session["id"],)).fetchone()
        indicator = json.loads(row["indicator_state_json"])
        assert indicator["count"] == 4 and indicator["last_t"] == row["last_t"]
        assert indicator["ema"] and indicator["macd"] and indicator["atr"]
        assert paper_indicator_state_matches(indicator, rules, json.loads(row["history_json"]), 80)
    assert verify_spot_paper_database(paper.path)["schema_version"] == 6

    with sqlite3.connect(paper.path) as db:
        db.execute("DROP TABLE session_candles")
        db.execute("ALTER TABLE sessions DROP COLUMN indicator_state_json")
        db.execute("PRAGMA user_version=3")
    reopened = SpotStrategyPaper(paper.path, paper.lab.path)
    assert reopened.detail(session["id"])["status"] == "ACTIVE"
    assert verify_spot_paper_database(paper.path)["schema_version"] == 6


def test_spot_paper_v3_legacy_indicator_precision_is_preserved(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    strategy = lab.create_strategy("EMA y MACD legados", {
        **definition(), "entry": {"op": "AND", "children": [
            {"kind": "CLOSE_ABOVE_EMA", "period": 2},
            {"kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 2,
             "slow_period": 3, "signal_period": 2}]}})
    session = paper.create(strategy["version_id"], dataset["id"])
    with sqlite3.connect(paper.path) as db:
        db.execute("DROP TABLE session_candles")
        db.execute("UPDATE sessions SET engine_version='lab-1.15' WHERE id=?", (session["id"],))
        db.execute("ALTER TABLE sessions DROP COLUMN indicator_state_json")
        db.execute("PRAGMA user_version=3")
    state["now"] = START + 4 * STEP + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    with paper.connect() as db:
        snapshot = json.loads(db.execute("SELECT indicator_state_json FROM sessions WHERE id=?",
                                         (session["id"],)).fetchone()[0])
    assert snapshot["precision"] == 28 and snapshot["ema"] and snapshot["macd"]


def test_spot_paper_uses_recent_shadow_at_512_candles(tmp_path):
    paper, state, lab, _, _ = harness(tmp_path)
    rows = state["rows"]
    rows[:] = [{"t": START + index * STEP, "o": str(100 + index % 7),
                "h": str(104 + index % 7), "l": str(96 + index % 7),
                "c": str(101 + index % 7), "v": str(10 + index % 11)}
               for index in range(513)]
    state["now"] = START + 512 * STEP + 10
    bars, gaps = validate_candles(rows[:512], "1h", now=state["now"])
    dataset = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars, gaps,
                               verified_public=True)
    strategy = lab.create_strategy("Ventana PAPER Spot", {
        **definition(), "entry": {"op": "AND", "children": [
            {"kind": "CLOSE_ABOVE_EMA", "period": 20},
            {"kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 12,
             "slow_period": 26, "signal_period": 9},
            {"kind": "ATR_PCT_GT", "period": 14, "threshold": "0.1"}]}})
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 513 * STEP + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED" and observed["status"] == "ACTIVE"
    assert observed["capacity_remaining"] == 5000 - 513


def test_spot_paper_rejects_divergent_indicator_snapshot(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    strategy = lab.create_strategy("EMA PAPER", {
        **definition(), "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}})
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        state_json = json.loads(db.execute("SELECT indicator_state_json FROM sessions WHERE id=?",
                                           (session["id"],)).fetchone()[0])
        state_json["ema"]["2"]["current"] = "999"
        db.execute("UPDATE sessions SET indicator_state_json=? WHERE id=?",
                   (json.dumps(state_json), session["id"]))
    with pytest.raises(SpotPaperAccessError, match="divergentes"):
        verify_spot_paper_database(paper.path)
    state["now"] = START + 4 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == "GAP" and result["events"] == []


def test_spot_paper_backup_rejects_altered_observed_candle(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    assert verify_spot_paper_database(paper.path)["counts"]["observed_candles"] == 1
    with sqlite3.connect(paper.path) as db:
        db.execute("""UPDATE observed_candles
                      SET candle_json=json_set(candle_json,'$.c','999') WHERE session_id=?""",
                   (session["id"],))
    with pytest.raises(SpotPaperAccessError, match="divergentes"):
        verify_spot_paper_database(paper.path)


def test_spot_paper_stops_before_acting_on_divergent_archive(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    first = paper.poll(session["id"])
    assert first["poll_result"] == "OBSERVED"
    with paper.connect() as db:
        db.execute("""UPDATE observed_candles SET candle_json=json_set(candle_json,'$.c','999')
                      WHERE session_id=?""", (session["id"],))
    state["now"] = START + 5 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == "GAP" and result["poll_result"] == "GAP"
    assert result["last_t"] == first["last_t"]
    assert len(result["events"]) == len(first["events"])


def test_spot_paper_archive_failure_rolls_back_observation_and_buy(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    before = paper.detail(session["id"])
    with paper.connect() as db:
        db.execute("""CREATE TRIGGER reject_paper_candle BEFORE INSERT ON observed_candles
                      BEGIN SELECT RAISE(ABORT,'archive unavailable'); END""")
    state["now"] = START + 4 * STEP + 1
    with pytest.raises(sqlite3.IntegrityError, match="archive unavailable"):
        paper.poll(session["id"])
    after = paper.detail(session["id"])
    assert after["events"] == []
    assert after["state"] == before["state"]
    assert after["last_t"] == before["last_t"]


def test_spot_paper_full_archive_failure_rolls_back_observation(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        before = db.execute("SELECT history_json,indicator_state_json FROM sessions WHERE id=?",
                            (session["id"],)).fetchone()
        db.execute("""CREATE TRIGGER reject_full_candle BEFORE INSERT ON session_candles
                      WHEN NEW.origin='OBSERVED'
                      BEGIN SELECT RAISE(ABORT,'full archive unavailable'); END""")
    state["now"] = START + 4 * STEP + 1
    with pytest.raises(sqlite3.IntegrityError, match="full archive unavailable"):
        paper.poll(session["id"])
    with paper.connect() as db:
        after = db.execute("SELECT history_json,indicator_state_json FROM sessions WHERE id=?",
                           (session["id"],)).fetchone()
        assert tuple(after) == tuple(before)
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM observed_candles").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM session_candles").fetchone()[0] == 3


def test_spot_paper_chain_failure_rolls_back_candle_signal_and_state(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        before = db.execute("SELECT history_json,state_json,indicator_state_json FROM sessions WHERE id=?",
                            (session["id"],)).fetchone()
        db.execute(f"""CREATE TRIGGER reject_chain BEFORE INSERT ON session_archive_chain
                       WHEN NEW.candle_t > {START + 2 * STEP}
                       BEGIN SELECT RAISE(ABORT,'chain unavailable'); END""")
    state["now"] = START + 4 * STEP + 1
    with pytest.raises(sqlite3.IntegrityError, match="chain unavailable"):
        paper.poll(session["id"])
    with paper.connect() as db:
        after = db.execute("SELECT history_json,state_json,indicator_state_json FROM sessions WHERE id=?",
                           (session["id"],)).fetchone()
        assert tuple(after) == tuple(before)
        for table in ("events", "observed_candles"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        for table in ("session_candles", "session_archive_chain"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 3


def test_spot_paper_indicator_snapshot_failure_rolls_back_observation(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        before = db.execute("SELECT indicator_state_json FROM sessions WHERE id=?",
                            (session["id"],)).fetchone()[0]
        db.execute("""CREATE TRIGGER reject_indicator_state BEFORE UPDATE OF indicator_state_json ON sessions
                      BEGIN SELECT RAISE(ABORT,'indicator archive unavailable'); END""")
    state["now"] = START + 4 * STEP + 1
    with pytest.raises(sqlite3.IntegrityError, match="indicator archive unavailable"):
        paper.poll(session["id"])
    with paper.connect() as db:
        assert db.execute("SELECT indicator_state_json FROM sessions WHERE id=?",
                          (session["id"],)).fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM observed_candles").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_spot_paper_restarts_in_new_process_locked_and_never_backfills_late_buy(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    password = "isolated spot restart password"
    spot_paper_access.create(paper.path, password)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    assert opened["poll_result"] == "OBSERVED"
    assert opened["events"][0]["action"]["side"] == "BUY"

    probe = """
import json
from pathlib import Path
import sys
from app.automation.spot_paper_access import spot_paper_access
from app.automation.spot_strategy_paper import SpotPaperLocked, SpotStrategyPaper
from app.automation.spot_strategy_worker import SpotStrategyWorker

class NoNetwork:
    def __init__(self):
        raise AssertionError("No debe consultar MEXC para ejecutar un intervalo perdido")

path, lab_path, session_id, now = sys.argv[1:]
paper = SpotStrategyPaper(Path(path), Path(lab_path), allow_create=False,
                          adapter_factory=NoNetwork, clock_ns=lambda: int(now) * 1_000_000_000)
assert spot_paper_access.status(Path(path))["unlocked"] is False
try:
    paper.detail(session_id)
except SpotPaperLocked:
    pass
else:
    raise AssertionError("La sesión no quedó bloqueada tras reiniciar")
spot_paper_access.unlock(Path(path), sys.stdin.read())
result = SpotStrategyWorker(paper).run_once()
detail = paper.detail(session_id)
print(json.dumps({"poll_result": result.get(session_id), "status": detail["status"],
                  "events": len(detail["events"]), "state": detail["state"]}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(paper.path), str(paper.lab.path),
         session["id"], str(START + 5 * STEP + 901)],
        input=password, capture_output=True, text=True, timeout=20, check=False,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr
    assert password not in result.stdout + result.stderr
    recovered = json.loads(result.stdout)
    assert recovered == {"poll_result": "GAP", "status": "GAP", "events": 2,
                         "state": opened["state"]}


def definition(direction="LONG"):
    return {"direction": direction, "initial_capital": "1000", "allocation": "0.1",
            "fee_rate": "0.001", "slippage_rate": "0", "stop_loss": "0.03",
            "take_profit": "0.08", "entry": {"kind": "BEARISH_STREAK", "period": 3}}


def harness(tmp_path, *, volumes=None):
    state = {"now": START + 3 * STEP + 10, "bid": "99", "ask": "100", "offline": False,
             "revised": False, "candle_calls": 0, "quote_calls": 0}
    closes = [100, 99, 98, 97, 110]
    rows = [{"t": START + i * STEP, "o": str(c + 1), "h": str(c + 2),
             "l": str(c - 1), "c": str(c), "v": str(volumes[i] if volumes else 10 + i)}
            for i, c in enumerate(closes)]
    state["rows"] = rows

    class Source:
        known_pairs = ("BTC/USDT",)

        def get_candles(self, symbol, timeframe="1h", limit=3, since=None, before=None):
            state["candle_calls"] += 1
            if state["offline"]:
                return None
            selected = [dict(row) for row in rows if row["t"] >= since]
            if state["revised"]:
                selected[0]["c"] = "98.5"
            return selected[:limit]

        def get_paper_quote(self, symbol):
            state["quote_calls"] += 1
            if state["offline"]:
                return None
            return {"symbol": symbol, "source": "mexc-spot-public",
                    "event_time_ns": state.get("quote_event_ns", state["now"] * 1_000_000_000),
                    "bid": state["bid"], "ask": state["ask"], "last": state["bid"]}

    lab_path, paper_path = tmp_path / "lab.db", tmp_path / "spot-paper.db"
    lab = LabStore(lab_path)
    bars, gaps = validate_candles(rows[:3], "1h", now=state["now"])
    dataset = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars, gaps,
                               verified_public=True)
    strategy = lab.create_strategy("Tres velas spot", definition())
    def clock_ns():
        ticks = state.get("clock_ticks")
        current = ticks.pop(0) if ticks else state["now"]
        return current * 1_000_000_000
    paper = SpotStrategyPaper(paper_path, lab_path, adapter_factory=Source,
                              clock_ns=clock_ns)
    return paper, state, lab, dataset, strategy


def test_spot_worker_skips_archive_replay_until_an_observation_is_due(tmp_path, monkeypatch):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    session_id = session["id"]
    inactive = lab.create_strategy("Detenida", definition())
    paper.stop(paper.create(inactive["version_id"], dataset["id"])["id"])
    worker = SpotStrategyWorker(paper)
    due = START + 4 * STEP

    def unexpected(*args, **kwargs):
        pytest.fail("El planificador leyó el archivo fuera de una observación")

    with monkeypatch.context() as scheduled:
        scheduled.setattr(paper, "_valid_session_state", unexpected)
        scheduled.setattr(paper, "_summary", unexpected)
        assert paper.scheduled_sessions() == [{"id": session_id, "due_at": due}]
        assert worker.run_once() == {}
        assert state["candle_calls"] == state["quote_calls"] == 0

    state["now"] = due + 1
    with monkeypatch.context() as scheduled:
        scheduled.setattr(paper, "detail", unexpected)
        assert worker.run_once() == {session_id: "OBSERVED"}
        assert paper.scheduled_sessions() == [{"id": session_id, "due_at": due + 6}]
        state["now"] += 5
        assert worker.run_once() == {session_id: "WAIT"}
    detail = paper.detail(session_id)
    assert detail["events"][0]["action"]["side"] == "BUY"
    assert len(detail["events"]) == 1
    assert detail["last_quote"]["checked_at_ns"] == (due + 6) * 1_000_000_000
    assert state["candle_calls"] == 1 and state["quote_calls"] == 2


@pytest.mark.parametrize("observation", ["bar", "intrabar", "manual_close"])
def test_spot_observation_started_before_reunlock_cannot_write(tmp_path, monkeypatch, observation):
    paper, state, _, dataset, strategy = harness(tmp_path)
    spot_paper_access.create(paper.path, SPOT_PAPER_PASSWORD)
    try:
        session_id = paper.create(strategy["version_id"], dataset["id"])["id"]
        state["now"] = START + 4 * STEP + 1
        if observation != "bar":
            assert paper.observe(session_id) == {"poll_result": "OBSERVED"}
            state.update(now=state["now"] + 5, bid="96", ask="97")
        before = paper.detail(session_id)
        source = paper.adapter_factory

        class RotatingSource(source):
            def get_paper_quote(self, symbol):
                quote = super().get_paper_quote(symbol)
                spot_paper_access.lock(paper.path)
                spot_paper_access.unlock(paper.path, SPOT_PAPER_PASSWORD)
                return quote

        check = paper.close_position if observation == "manual_close" else paper.observe
        with monkeypatch.context() as rotated:
            rotated.setattr(paper, "adapter_factory", RotatingSource)
            with pytest.raises(SpotPaperLocked, match="cambió"):
                check(session_id)
        assert paper.detail(session_id) == before
        check(session_id)
        after = paper.detail(session_id)
        assert len(after["events"]) == len(before["events"]) + 1
        assert after["events"][-1]["action"]["side"] == ("BUY" if observation == "bar" else "SELL")
    finally:
        spot_paper_access.lock(paper.path)


def test_spot_observation_rolls_back_if_access_is_revoked_inside_write(tmp_path, monkeypatch):
    paper, state, _, dataset, strategy = harness(tmp_path)
    spot_paper_access.create(paper.path, SPOT_PAPER_PASSWORD)
    try:
        session_id = paper.create(strategy["version_id"], dataset["id"])["id"]
        state["now"] = START + 4 * STEP + 1
        before = paper.detail(session_id)
        advance = paper._advance

        def revoke(*args, **kwargs):
            spot_paper_access.lock(paper.path)
            return advance(*args, **kwargs)

        with monkeypatch.context() as revoked:
            revoked.setattr(paper, "_advance", revoke)
            with pytest.raises(SpotPaperLocked, match="cambió"):
                paper.observe(session_id)
        spot_paper_access.unlock(paper.path, SPOT_PAPER_PASSWORD)
        assert paper.detail(session_id) == before
        assert paper.observe(session_id) == {"poll_result": "OBSERVED"}
    finally:
        spot_paper_access.lock(paper.path)


def test_spot_worker_uses_legacy_quote_time_and_stops_on_invalid_risk_time(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session_id = paper.create(strategy["version_id"], dataset["id"])["id"]
    state["now"] = START + 4 * STEP + 1
    state["quote_event_ns"] = (state["now"] + 1) * 1_000_000_000
    opened = paper.poll(session_id)
    with paper.connect() as db:
        db.execute("UPDATE sessions SET last_quote_json=json_remove(last_quote_json,'$.checked_at_ns') WHERE id=?",
                   (session_id,))
    assert paper.scheduled_sessions() == [{"id": session_id, "due_at": state["now"] + 6}]
    with paper.connect() as db:
        db.execute("UPDATE sessions SET last_quote_json='[]' WHERE id=?", (session_id,))
    assert paper.scheduled_sessions() == [{"id": session_id, "due_at": 0}]
    assert SpotStrategyWorker(paper).run_once() == {session_id: "GAP"}
    stopped = paper.detail(session_id)
    assert stopped["status_reason"] == paper.INVALID_STATE_REASON
    assert stopped["state"] == opened["state"] and stopped["events"] == opened["events"]
    assert stopped["valuation"]["basis"] == "UNAVAILABLE_INVALID_STATE"
    assert state["quote_calls"] == 1


def test_spot_paper_requests_exact_mexc_closed_window_when_open_candle_exists(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    due = START + 4 * STEP
    state["now"] = due + 1
    queries = []

    def mexc_get(url):
        target = urlsplit(url)
        query = parse_qs(target.query)
        assert query["symbol"] == ["BTCUSDT"]
        if target.path == "/api/v3/ticker/24hr":
            return {"symbol":"BTCUSDT","closeTime":state["now"]*1000,"bidPrice":"99",
                    "askPrice":"100","lastPrice":"99"}
        assert target.path == "/api/v3/klines"
        queries.append(query)
        if "endTime" not in query:
            selected = state["rows"][-3:]  # MEXC can return the recent window and include the open candle.
        else:
            start = int(query["startTime"][0])
            end = int(query["endTime"][0])
            selected = [bar for bar in state["rows"] if start <= bar["t"]*1000 <= end]
        return [[bar["t"]*1000,*(bar[key] for key in ("o","h","l","c","v"))] for bar in selected]

    paper.adapter_factory = lambda: MexcAdapter(http_get=mexc_get)
    result = paper.poll(session["id"])
    assert result["poll_result"] == "OBSERVED" and result["events"][0]["action"]["side"] == "BUY"
    assert result["events"][0]["candle_t"] == due - STEP
    assert queries == [{"symbol":["BTCUSDT"],"interval":["60m"],"limit":["3"],
                        "startTime":[str((due-3*STEP)*1000)],"endTime":[str(due*1000-1)]}]


def test_spot_paper_waits_for_quote_after_signal_candle_close(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    due = START + 4 * STEP
    state["now"] = due + 1
    state["quote_event_ns"] = (due - 5) * 1_000_000_000

    with pytest.raises(SpotStrategyUnavailable, match="anterior al cierre"):
        paper.poll(session["id"])
    pending = paper.detail(session["id"])
    assert pending["status"] == "ACTIVE" and pending["events"] == []
    assert pending["state"]["units"] == "0"

    state["quote_event_ns"] = (due + 1) * 1_000_000_000
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    assert observed["events"][0]["action"]["side"] == "BUY"


def test_ascending_volume_signal_agrees_between_lab_and_spot_paper(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path, volumes=(50, 10, 20, 30, 40))
    rule = {"kind": "VOLUME_ASCENDING_BARS", "period": 3}
    strategy = lab.create_strategy("Volumen de tres velas", validate_strategy({
        **definition(), "entry": rule}))
    session = paper.create(strategy["version_id"], dataset["id"])
    bars, _ = validate_candles(state["rows"], "1h", now=START + 6 * STEP)
    lab_result = run_backtest(bars, strategy["definition"], "1h")
    assert any(event["signal_time"] == START + 3 * STEP
               and event["action"] == "ENTER_NEXT_OPEN" for event in lab_result["events"])
    assert evaluate({"kind": "VOLUME_RISING_STREAK", "period": 3}, bars, 3)[0] is False
    state["now"] = START + 4 * STEP + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    assert observed["events"][0]["entry_match"] is True
    assert observed["events"][0]["action"]["side"] == "BUY"
    assert paper.comparison(session["id"])["signal_replay"]["verified"] is True


def test_spot_paper_trailing_stop_uses_closed_bar_high_and_observed_bid(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    version = lab.create_strategy("Stop móvil spot", {
        **definition(), "stop_loss": "0.03", "take_profit": "0.5",
        "trailing_stop": "0.05"})
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    assert opened["state"]["trailing_extreme"] == opened["state"]["entry_price"]
    assert opened["events"][0]["action"]["side"] == "BUY"
    keep_risk_covered(paper, state, session["id"], START + 5 * STEP + 1)
    state["now"] = START + 5 * STEP + 1
    state["bid"], state["ask"] = "110", "111"
    advanced = paper.poll(session["id"])
    assert advanced["poll_result"] == "OBSERVED"
    assert advanced["state"]["trailing_extreme"] == "112"
    assert advanced["state"]["units"] != "0"
    state["now"] += 5
    state["bid"], state["ask"] = "106", "107"
    closed = paper.poll(session["id"])
    assert closed["poll_result"] == "RISK_EXIT"
    assert closed["events"][-1]["action"]["reason"] == "TRAILING_STOP_OBSERVED_BID"
    assert closed["state"]["trailing_extreme"] is None
    assert closed["state"]["units"] == "0"
    assert SpotStrategyPaper(paper.path, paper.lab.path).detail(session["id"])["state"] == closed["state"]


def test_spot_paper_trailing_stop_does_not_use_current_bar_high_for_same_bar_exit():
    strategy = validate_strategy({**definition(), "stop_loss": "0.2",
                                  "take_profit": "0.5", "trailing_stop": "0.05"})
    state = {"cash": "900", "units": "1", "entry_price": "100",
             "entry_cost": "100", "trailing_extreme": "100",
             "realized_pnl": "0", "closed_trades": 0}
    advanced, action = SpotStrategyPaper._advance(
        state, strategy, entry=False, exit_match=False, signal_close=Decimal("101"),
        signal_high=Decimal("120"), signal_t=START + 4 * STEP,
        step=STEP, bid=Decimal("101"), ask=Decimal("102"))
    assert action is None
    assert advanced["trailing_extreme"] == "120"
    closed, action = SpotStrategyPaper._advance(
        advanced, strategy, entry=False, exit_match=False, signal_close=Decimal("113"),
        signal_high=Decimal("115"), signal_t=START + 5 * STEP,
        step=STEP, bid=Decimal("113"), ask=Decimal("114"))
    assert action["reason"] == "TRAILING_STOP_CLOSE"
    assert closed["trailing_extreme"] is None


def test_spot_paper_trailing_stop_gap_never_backfills_an_exit(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    version = lab.create_strategy("Stop móvil sin reconstrucción", {
        **definition(), "take_profit": "0.5", "trailing_stop": "0.05"})
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    state["now"] += 31
    state["bid"], state["ask"] = "90", "91"
    gap = paper.poll(session["id"])
    assert gap["poll_result"] == "GAP"
    assert gap["state"] == opened["state"]
    assert gap["events"][-1]["kind"] == "RISK_COVERAGE_GAP"
    assert all(not event.get("action") or event["action"]["side"] != "SELL"
               for event in gap["events"])


def test_spot_paper_trailing_stop_corrupt_extreme_fails_closed(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    version = lab.create_strategy("Stop móvil íntegro", {
        **definition(), "take_profit": "0.5", "trailing_stop": "0.05"})
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    with paper.connect() as db:
        db.execute("UPDATE sessions SET state_json=json_set(state_json,'$.trailing_extreme','NaN') WHERE id=?",
                   (session["id"],))
    state["now"] += 5
    result = paper.poll(session["id"])
    assert result["status"] == "GAP"
    assert result["state"]["units"] != "0"
    assert result["state"]["closed_trades"] == 0


@pytest.mark.parametrize("cash", ("NaN", "-1", "1200"))
def test_spot_paper_rejects_impossible_flat_cash_before_market_fetch(tmp_path, cash):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("UPDATE sessions SET state_json=json_set(state_json,'$.cash',?) WHERE id=?",
                   (cash, session["id"]))
    state["now"] = START + 4 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert "Estado PAPER spot" in result["status_reason"]
    assert result["events"] == []
    assert state["candle_calls"] == state["quote_calls"] == 0


def test_spot_paper_rejects_plausible_bookkeeping_without_matching_events(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        # 900 - (-100) = 1000: the basic capital equation still balances.
        db.execute("""UPDATE sessions SET state_json=json_set(
                   state_json,'$.cash','900','$.realized_pnl','-100') WHERE id=?""",
                   (session["id"],))
    state["now"] = START + 4 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert result["valuation"]["basis"] == "UNAVAILABLE_INVALID_STATE"
    assert result["events"] == []
    assert state["candle_calls"] == state["quote_calls"] == 0


def test_spot_paper_rejects_missing_buy_event_before_intrabar_decision(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    assert opened["state"]["units"] != "0"
    with paper.connect() as db:
        db.execute("UPDATE events SET payload_json=json_set(payload_json,'$.action',NULL) WHERE session_id=?",
                   (session["id"],))
    state["now"] += 5
    before_quotes = state["quote_calls"]
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert result["valuation"]["basis"] == "UNAVAILABLE_INVALID_STATE"
    assert state["quote_calls"] == before_quotes


def test_spot_paper_rejects_event_changed_during_quote(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    source = paper.adapter_factory

    class MutatingSource(source):
        def get_paper_quote(self, symbol):
            quote = super().get_paper_quote(symbol)
            with paper.connect() as db:
                db.execute("UPDATE events SET payload_json=json_set(payload_json,'$.action',NULL) WHERE session_id=?",
                           (session["id"],))
            return quote

    paper.adapter_factory = MutatingSource
    state["now"] += 5
    state["bid"], state["ask"] = "90", "91"
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert result["valuation"]["basis"] == "UNAVAILABLE_INVALID_STATE"
    assert len(result["events"]) == 1


def test_spot_paper_unreadable_event_remains_inspectable(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    with paper.connect() as db:
        db.execute("UPDATE events SET payload_json='{' WHERE session_id=?", (session["id"],))
    result = paper.detail(session["id"])
    assert result["valuation"]["basis"] == "UNAVAILABLE_INVALID_STATE"
    assert result["events"][0]["unreadable"] is True
    assert result["events"][0]["action"] is None


def test_spot_paper_unreadable_state_remains_inspectable_without_valuation(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("UPDATE sessions SET state_json='{' WHERE id=?", (session["id"],))
    state["now"] = START + 4 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert result["state"] is None
    assert result["valuation"]["basis"] == "UNAVAILABLE_INVALID_STATE"
    assert result["valuation"]["equity_if_closed"] is None
    assert result["events"] == []
    assert state["candle_calls"] == state["quote_calls"] == 0


def test_spot_paper_rejects_state_changed_during_quote(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    source = paper.adapter_factory

    class MutatingSource(source):
        def get_paper_quote(self, symbol):
            quote = super().get_paper_quote(symbol)
            with paper.connect() as db:
                saved = json.loads(db.execute(
                    "SELECT state_json FROM sessions WHERE id=?", (session["id"],)).fetchone()[0])
                db.execute("UPDATE sessions SET state_json=? WHERE id=?",
                           (json.dumps(saved | {"cash": "900", "realized_pnl": "-100",
                                                "closed_trades": 1}), session["id"]))
            return quote

    paper.adapter_factory = MutatingSource
    state["now"] = START + 4 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert "Estado PAPER spot" in result["status_reason"]
    assert result["events"] == []
    assert state["candle_calls"] == state["quote_calls"] == 1


def test_spot_paper_rejects_state_changed_during_intrabar_quote(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    assert opened["state"]["units"] != "0"
    source = paper.adapter_factory

    class MutatingSource(source):
        def get_paper_quote(self, symbol):
            quote = super().get_paper_quote(symbol)
            with paper.connect() as db:
                db.execute("UPDATE sessions SET state_json=json_set(state_json,'$.cash','800') WHERE id=?",
                           (session["id"],))
            return quote

    paper.adapter_factory = MutatingSource
    state["now"] += 5
    state["bid"] = "90"
    state["ask"] = "91"
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert len(result["events"]) == 1
    assert result["state"]["units"] == opened["state"]["units"]


def test_spot_paper_manual_close_rejects_invalid_state_before_quote(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    before_quotes = state["quote_calls"]
    with paper.connect() as db:
        db.execute("UPDATE sessions SET state_json=json_set(state_json,'$.cash','NaN') WHERE id=?",
                   (session["id"],))
    with pytest.raises(SpotStrategyError, match="Estado PAPER spot"):
        paper.close_position(session["id"])
    assert state["quote_calls"] == before_quotes
    assert len(paper.detail(session["id"])["events"]) == 1


def test_spot_gap_analysis_reports_missed_signals_without_paper_actions(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with pytest.raises(LabError, match="hueco"):
        paper.gap_analysis(session["id"])
    state["now"] = START + 5 * STEP + 10
    assert paper.poll(session["id"])["poll_result"] == "GAP"
    before = paper.detail(session["id"])
    report = paper.gap_analysis(session["id"])
    assert report["status"] == "RECONSTRUCTED_NO_EXECUTION"
    assert report["candles_reviewed"] == 2
    assert report["entry_signals"] == 2
    assert report["signals"][0]["candle_t"] == START + 3 * STEP
    assert report["paper_state_unchanged"] is True
    assert len(report["archive"]["hash"]) == 64
    assert paper.detail(session["id"]) == before
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    app.include_router(create_lab_router(paper.lab.path))
    assert TestClient(app).get(
        f"/paper/spot/strategies/{session['id']}/gap-analysis").json() == report
    recovered = SpotStrategyPaper(paper.path, paper.lab.path)
    assert recovered.gap_analyses(session["id"])[0]["result"]["signals"] == report["signals"]
    saved = paper.lab.paper_gap_report(report["archive"]["id"])
    assert saved["result"]["candles_reviewed"] == 2
    api_saved = TestClient(app).get(f"/lab/paper-gap-reports/{saved['id']}")
    assert api_saved.status_code == 200 and api_saved.json()["hash"] == saved["hash"]
    assert api_saved.headers["cache-control"] == "no-store"
    backup = create_lab_backup(paper.lab)
    assert verify_lab_backup(Path(backup["path"]))["counts"]["paper_gap_reports"] == 1
    with sqlite3.connect(backup["path"]) as db:
        db.execute("UPDATE paper_gap_reports SET result_json=? WHERE id=?",
                   ('{"entry_signals":999}', report["archive"]["id"]))
    with pytest.raises(LabError, match="reconstrucción"):
        verify_lab_backup(Path(backup["path"]))
    state["revised"] = True
    with pytest.raises(LabError):
        paper.gap_analysis(session["id"])


SPOT_PAPER_PASSWORD = "Spot paper test secret 123"


def test_spot_readiness_matches_create_freshness_without_creating_session(tmp_path):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, lab.path, paper=paper))
    client = TestClient(app)
    endpoint = f"/paper/spot/strategies/readiness?dataset_id={dataset['id']}"
    assert client.get(endpoint).json()["ready"] is True
    assert paper.sessions() == []
    state["now"] += STEP
    stale = client.get(endpoint)
    assert stale.status_code == 200 and stale.json()["ready"] is False
    assert "última vela cerrada" in stale.json()["reason"]
    with pytest.raises(SpotStrategyError, match="última vela cerrada"):
        paper.create(strategy["version_id"], dataset["id"])
    assert paper.sessions() == []


def test_spot_paper_rejects_unreachable_higher_timeframe_before_creating_session(tmp_path):
    paper, _, lab, dataset, _ = harness(tmp_path)
    version = lab.create_strategy("EMA diaria imposible", {
        **definition(), "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 300,
                                   "timeframe": "1d"}})
    with pytest.raises(LabError, match="necesita 300 velas 1d completas"):
        paper.create(version["version_id"], dataset["id"])
    assert paper.sessions() == []


def test_spot_paper_detail_reports_higher_timeframe_warmup_as_candles_arrive(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    version = lab.create_strategy("EMA 4h", {
        **definition(), "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2,
                                   "timeframe": "4h"}})
    session = paper.create(version["version_id"], dataset["id"])
    first = session["coverage"]["rules"][0]
    assert (first["complete_bars"], first["minimum_bars"], first["status"]) == (0, 2, "INSUFFICIENT")
    state["now"] = START + 4 * STEP + 10
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    current = paper.detail(session["id"])["coverage"]["rules"][0]
    assert (current["complete_bars"], current["minimum_bars"], current["status"]) == (1, 2, "INSUFFICIENT")


def test_spot_paper_clock_rollback_after_quote_does_not_book_signal(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    due = START + 4 * STEP
    state["now"] = due + 10
    state["clock_ticks"] = [due + 10] * 4 + [due - 1]
    with pytest.raises(SpotStrategyUnavailable, match="Reloj local retrocedió"):
        paper.poll(session["id"])
    unchanged = paper.detail(session["id"])
    assert unchanged["status"] == "ACTIVE" and unchanged["events"] == []
    state["now"] = due + 11
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"


def test_spot_paper_intrabar_clock_rollback_does_not_close_position(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    assert opened["state"]["units"] != "0"
    state["now"] += 5
    state["bid"] = "96"
    state["ask"] = "97"
    state["clock_ticks"] = [state["now"], state["now"], state["now"] - 1]
    with pytest.raises(SpotStrategyUnavailable, match="Reloj local retrocedió"):
        paper.poll(session["id"])
    unchanged = paper.detail(session["id"])
    assert unchanged["status"] == "ACTIVE"
    assert unchanged["state"]["units"] == opened["state"]["units"]
    assert len(unchanged["events"]) == 1
    state["now"] += 1
    assert paper.poll(session["id"])["poll_result"] == "RISK_EXIT"


def test_spot_paper_source_verification_detects_changed_version_without_changing_session(tmp_path):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    assert paper.verify_source(session["id"])["verified"] is True
    state["now"] = START + 4 * STEP + 10
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    assert paper.verify_source(session["id"])["verified"] is True
    before = paper.detail(session["id"])
    with lab.connect() as db:
        changed = {**strategy["definition"], "take_profit": "0.3"}
        db.execute("UPDATE strategy_versions SET definition_json=? WHERE id=?",
                   (json.dumps(changed), strategy["version_id"]))
    verified = paper.verify_source(session["id"])
    assert verified["status"] == "mismatch" and verified["checks"]["strategy"] is False
    assert verified["checks"]["dataset"] is True
    assert paper.detail(session["id"]) == before


def test_spot_paper_source_api_rejects_changed_seed_without_rewriting_paper(tmp_path):
    paper, _, lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, lab.path, paper=paper))
    client = TestClient(app)
    endpoint = f"/paper/spot/strategies/{session['id']}/verify"
    assert client.get(endpoint).json()["verified"] is True
    with paper.connect() as db:
        history = json.loads(db.execute("SELECT history_json FROM sessions WHERE id=?",
                                        (session["id"],)).fetchone()[0])
        history[0]["v"] = "999"
        db.execute("UPDATE sessions SET history_json=? WHERE id=?",
                   (json.dumps(history), session["id"]))
    result = client.get(endpoint)
    assert result.status_code == 200
    assert result.json()["status"] == "mismatch"
    assert result.json()["checks"]["seed"] is False
    assert paper.sessions()[0]["status"] == "ACTIVE"


def test_spot_paper_encryption_preserves_observed_session_and_can_restore(tmp_path):
    paper, state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    original = paper.detail(session["id"])
    before = verify_spot_paper_database(paper.path)
    assert before["counts"] == {"sessions": 1, "events": 1, "observed_candles": 1,
                                 "session_candles": 4, "session_archive_chain": 4}

    result = encrypt_active_spot_paper(paper, "CIFRAR SPOT PAPER", SPOT_PAPER_PASSWORD)
    assert result["verified"] == before
    assert spot_paper_format(paper.path) == "encrypted_or_invalid"
    with pytest.raises(SpotStrategyError, match="bloqueado"):
        paper.sessions()

    wake = []
    api = FastAPI()
    api.include_router(create_spot_strategy_router(
        paper.path, paper.lab.path, paper=paper,
        on_access_change=lambda: wake.append(True)))
    client = TestClient(api)
    assert client.get("/paper/spot/strategies").status_code == 423
    assert client.post("/paper/spot/strategies/unlock", json={
        "passphrase": "wrong password 12345"}).status_code == 422
    assert client.post("/paper/spot/strategies/unlock", json={
        "passphrase": SPOT_PAPER_PASSWORD}).status_code == 200
    assert wake == [True]
    assert client.get("/paper/spot/strategies").json()[0]["id"] == session["id"]
    assert paper.detail(session["id"]) == original
    with paper.connect() as db:
        db.execute("UPDATE sessions SET status='STOPPED' WHERE id=?", (session["id"],))
    assert paper.detail(session["id"])["status"] == "STOPPED"
    assert client.post("/paper/spot/strategies/lock").status_code == 200
    assert wake == [True, True]
    assert client.get("/paper/spot/strategies").status_code == 423

    restored = restore_secure_spot_paper(
        paper, result["backup"], "RESTAURAR SPOT PAPER", SPOT_PAPER_PASSWORD)
    assert restored["verified"] == before
    assert restored["previous_backup"]
    assert verify_spot_paper_database(paper.path, SPOT_PAPER_PASSWORD) == before
    assert client.post("/paper/spot/strategies/unlock", json={
        "passphrase": SPOT_PAPER_PASSWORD}).status_code == 200
    assert paper.detail(session["id"]) == original
    spot_paper_access.lock(paper.path)


def test_spot_paper_encryption_rejects_open_store_and_unmanaged_restore(tmp_path):
    paper, _state, _lab, dataset, strategy = harness(tmp_path)
    paper.create(strategy["version_id"], dataset["id"])
    before = verify_spot_paper_database(paper.path)
    with spot_paper_lease(paper.path):
        with pytest.raises(SpotPaperAccessError, match="Orbis sigue usando"):
            encrypt_active_spot_paper(paper, "CIFRAR SPOT PAPER", SPOT_PAPER_PASSWORD)
    with sqlite3.connect(paper.path) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM sessions").fetchall()
        with pytest.raises(SpotPaperAccessError, match="Otro proceso usa"):
            encrypt_active_spot_paper(paper, "CIFRAR SPOT PAPER", SPOT_PAPER_PASSWORD)
        reader.rollback()
    assert verify_spot_paper_database(paper.path) == before
    result = encrypt_active_spot_paper(paper, "CIFRAR SPOT PAPER", SPOT_PAPER_PASSWORD)
    with pytest.raises(SpotPaperAccessError, match="Selecciona"):
        restore_secure_spot_paper(paper, "../outside.db", "RESTAURAR SPOT PAPER", SPOT_PAPER_PASSWORD)
    with pytest.raises(SpotPaperAccessError, match="verificar"):
        restore_secure_spot_paper(paper, result["backup"], "RESTAURAR SPOT PAPER",
                                  "wrong password 12345")
    spot_paper_access.unlock(paper.path, SPOT_PAPER_PASSWORD)
    with pytest.raises(SpotPaperAccessError, match="Bloquea"):
        restore_secure_spot_paper(paper, result["backup"], "RESTAURAR SPOT PAPER",
                                  SPOT_PAPER_PASSWORD)
    spot_paper_access.lock(paper.path)


def test_spot_migration_rejects_changed_bytes_with_restored_metadata(tmp_path, monkeypatch):
    paper, _state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    original = paper.path.stat()
    from app.automation import spot_paper_migration
    actual_sync = spot_paper_migration._sync_dir
    changed = False

    def changed_after_backup(directory):
        nonlocal changed
        actual_sync(directory)
        if changed:
            return
        changed = True
        with sqlite3.connect(paper.path) as db:
            old = db.execute("SELECT status FROM sessions WHERE id=?",
                             (session["id"],)).fetchone()[0]
            db.execute("UPDATE sessions SET status=? WHERE id=?",
                       ("X" * len(old), session["id"]))
        assert paper.path.stat().st_size == original.st_size
        os.utime(paper.path, ns=(original.st_atime_ns, original.st_mtime_ns))

    monkeypatch.setattr(spot_paper_migration, "_sync_dir", changed_after_backup)
    with pytest.raises(SpotPaperAccessError, match="cambió durante la migración"):
        encrypt_active_spot_paper(paper, "CIFRAR SPOT PAPER", SPOT_PAPER_PASSWORD)
    assert spot_paper_format(paper.path) == "plain"
    assert not list(tmp_path.glob(".spot-paper.db.encrypt-*"))


def test_spot_restore_rejects_changed_active_after_preimage(tmp_path, monkeypatch):
    paper, _state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    migrated = encrypt_active_spot_paper(paper, "CIFRAR SPOT PAPER", SPOT_PAPER_PASSWORD)
    from app.automation import spot_paper_migration
    actual_sync = spot_paper_migration._sync_dir
    changed = False

    def changed_after_preimage(directory):
        nonlocal changed
        actual_sync(directory)
        if changed:
            return
        changed = True
        original = paper.path.stat()
        with sqlcipher3.connect(paper.path) as db:
            db.execute("PRAGMA key = '" + SPOT_PAPER_PASSWORD + "'")
            old = db.execute("SELECT status FROM sessions WHERE id=?",
                             (session["id"],)).fetchone()[0]
            db.execute("UPDATE sessions SET status=? WHERE id=?",
                       ("X" * len(old), session["id"]))
        assert paper.path.stat().st_size == original.st_size
        os.utime(paper.path, ns=(original.st_atime_ns, original.st_mtime_ns))

    monkeypatch.setattr(spot_paper_migration, "_sync_dir", changed_after_preimage)
    with pytest.raises(SpotPaperAccessError, match="cambió durante la restauración"):
        restore_secure_spot_paper(paper, migrated["backup"], "RESTAURAR SPOT PAPER",
                                  SPOT_PAPER_PASSWORD)
    assert verify_spot_paper_database(paper.path, SPOT_PAPER_PASSWORD)["counts"]["sessions"] == 1
    assert not list(tmp_path.glob(".spot-paper.db.restore-*"))


def test_spot_paper_access_expires_without_creating_plain_database(tmp_path):
    paper, _state, _lab, dataset, strategy = harness(tmp_path)
    paper.create(strategy["version_id"], dataset["id"])
    encrypt_active_spot_paper(paper, "CIFRAR SPOT PAPER", SPOT_PAPER_PASSWORD)
    clock = [10.0]
    access = SpotPaperAccess(timeout_seconds=30, clock=lambda: clock[0])
    assert access.unlock(paper.path, SPOT_PAPER_PASSWORD)["unlocked"]
    clock[0] = 41.0
    assert access.get(paper.path) is None
    assert not access.status(paper.path)["unlocked"]
    assert spot_paper_format(paper.path) == "encrypted_or_invalid"


def test_spot_paper_encrypt_restore_cli_requires_confirmation(tmp_path):
    paper, _state, _lab, dataset, strategy = harness(tmp_path)
    paper.create(strategy["version_id"], dataset["id"])
    script = Path(__file__).resolve().parents[1] / "scripts" / "encrypt_spot_paper.py"
    command = [sys.executable, str(script), "--paper-db", str(paper.path),
               "--lab-db", str(paper.lab.path), "--passphrase-stdin"]
    rejected = subprocess.run(command, input=SPOT_PAPER_PASSWORD + "\n" + SPOT_PAPER_PASSWORD + "\n",
                              text=True, capture_output=True, check=False)
    assert rejected.returncode == 1
    assert spot_paper_format(paper.path) == "plain"
    accepted = subprocess.run(command + ["--confirm", "CIFRAR SPOT PAPER"],
                              input=SPOT_PAPER_PASSWORD + "\n" + SPOT_PAPER_PASSWORD + "\n",
                              text=True, capture_output=True, check=False)
    assert accepted.returncode == 0, accepted.stderr
    assert SPOT_PAPER_PASSWORD not in accepted.stdout
    backup = json.loads(accepted.stdout)["backup"]
    with SpotStrategyPaper(paper.path, paper.lab.path, passphrase=SPOT_PAPER_PASSWORD).connect() as db:
        db.execute("UPDATE sessions SET status='STOPPED'")
    restore = subprocess.run(
        [sys.executable, str(script.with_name("restore_spot_paper.py")),
         "--paper-db", str(paper.path), "--lab-db", str(paper.lab.path),
         "--restore", backup, "--confirm", "RESTAURAR SPOT PAPER", "--passphrase-stdin"],
        input=SPOT_PAPER_PASSWORD + "\n", text=True, capture_output=True, check=False)
    assert restore.returncode == 0, restore.stderr
    assert SPOT_PAPER_PASSWORD not in restore.stdout
    with SpotStrategyPaper(paper.path, paper.lab.path, passphrase=SPOT_PAPER_PASSWORD).connect() as db:
        assert db.execute("SELECT status FROM sessions").fetchone()[0] == "ACTIVE"


def test_spot_paper_comparison_links_verified_lab_without_false_return_delta(tmp_path):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    empty = paper.comparison(session["id"])
    assert empty["historical"] is None
    assert empty["paper"]["observed_candles"] == 0
    assert empty["signal_replay"]["status"] == "no_observations"
    bars, gaps = validate_candles(state["rows"][:3], "1h", now=state["now"])
    assert not gaps
    result = run_backtest(bars, strategy["definition"], "1h")
    run = lab.save_run(dataset["id"], strategy["version_id"], result, CURRENT_ENGINE_VERSION)
    state["now"] = START + 4 * STEP + 1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    compared = paper.comparison(session["id"])
    assert compared["historical"]["run_id"] == run["id"]
    assert compared["historical"]["verification"]["verified"] is True
    assert compared["historical"]["metrics"] == run["metrics"]
    assert compared["paper"]["observed_candles"] == 1
    assert compared["paper"]["paper_buys"] == 1
    assert compared["paper"]["paper_sells"] == 0
    assert compared["signal_replay"]["verified"] is True
    assert compared["signal_replay"]["checked_observations"] == 1
    assert compared["signal_frequency"]["historical"]["evaluated_candles"] == len(result["equity"])
    assert compared["signal_frequency"]["historical"]["signal_bursts"] == len(result["events"])
    assert compared["signal_frequency"]["paper"]["evaluated_candles"] == 1
    assert compared["signal_frequency"]["paper"]["first_candle_t"] == compared["signal_frequency"]["paper"]["last_candle_t"]
    assert compared["signal_frequency"]["paper"]["signal_bursts"] == 1
    assert compared["signal_frequency"]["directly_comparable"] is False
    assert compared["comparison_basis"] == "NOT_DIRECTLY_COMPARABLE"
    assert "delta" not in compared and "return_pct" not in compared["paper"]


def test_spot_comparison_recovers_lab_reference_after_seed_refresh(tmp_path):
    paper, state, lab, parent, strategy = harness(tmp_path)
    _, parent_rows = lab.dataset(parent["id"])
    parent_bars, _ = validate_candles(parent_rows, "1h", now=state["now"])
    run = lab.save_run(parent["id"], strategy["version_id"],
                       run_backtest(parent_bars, strategy["definition"], "1h"),
                       CURRENT_ENGINE_VERSION)
    changed = [dict(row) for row in parent_rows]
    changed[0]["v"] = "99"
    fresh_bars, _ = validate_candles(changed, "1h", now=state["now"])
    child = lab.save_refreshed_dataset(parent["id"], parent["hash"],
                                       parent["symbol"], parent["timeframe"],
                                       parent["source"], fresh_bars)
    session = paper.create(strategy["version_id"], child["id"])
    compared = paper.comparison(session["id"])
    assert compared["historical"]["run_id"] == run["id"]
    assert compared["historical"]["dataset_id"] == parent["id"]
    assert compared["historical"]["dataset_relation"] == "REFRESHED_FROM"
    assert compared["historical"]["refresh_hops"] == 1
    assert compared["historical"]["verification"]["verified"] is True
    assert compared["comparison_basis"] == "NOT_DIRECTLY_COMPARABLE"
    changed[1]["v"] = "98"
    newer_bars, _ = validate_candles(changed, "1h", now=state["now"])
    grandchild = lab.save_refreshed_dataset(child["id"], child["hash"],
                                            child["symbol"], child["timeframe"],
                                            child["source"], newer_bars)
    later = paper.create(strategy["version_id"], grandchild["id"])
    later_comparison = paper.comparison(later["id"])
    assert later_comparison["historical"]["run_id"] == run["id"]
    assert later_comparison["historical"]["refresh_hops"] == 2
    with lab.connect() as db:
        db.execute("UPDATE dataset_refreshes SET parent_hash=? WHERE child_id=?",
                   ("0" * 64, child["id"]))
    with pytest.raises(LabError, match="procedencia"):
        paper.comparison(later["id"])
    with lab.connect() as db:
        db.execute("UPDATE dataset_refreshes SET parent_hash=? WHERE child_id=?",
                   (parent["hash"], child["id"]))
    nearer = lab.save_run(child["id"], strategy["version_id"],
                          run_backtest(fresh_bars, strategy["definition"], "1h"),
                          CURRENT_ENGINE_VERSION)
    nearest_comparison = paper.comparison(later["id"])
    assert nearest_comparison["historical"]["run_id"] == nearer["id"]
    assert nearest_comparison["historical"]["refresh_hops"] == 1
    exact = lab.save_run(grandchild["id"], strategy["version_id"],
                         run_backtest(newer_bars, strategy["definition"], "1h"),
                         CURRENT_ENGINE_VERSION)
    exact_comparison = paper.comparison(later["id"])
    assert exact_comparison["historical"]["run_id"] == exact["id"]
    assert exact_comparison["historical"]["dataset_relation"] == "EXACT"
    assert exact_comparison["historical"]["refresh_hops"] == 0


def test_spot_paper_pins_verified_lab_run_and_never_silently_replaces_it(tmp_path):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    bars, _ = validate_candles(state["rows"][:3], "1h", now=state["now"])
    result = run_backtest(bars, strategy["definition"], "1h")
    origin = lab.save_run(dataset["id"], strategy["version_id"], result, CURRENT_ENGINE_VERSION)
    session = paper.create(strategy["version_id"], dataset["id"], origin["id"])
    assert session["source_run_id"] == origin["id"]
    newer = lab.save_run(dataset["id"], strategy["version_id"], result, CURRENT_ENGINE_VERSION)
    assert paper.comparison(session["id"])["historical"]["run_id"] == origin["id"]
    assert paper.comparison(session["id"])["historical"]["run_id"] != newer["id"]
    unrelated = lab.create_strategy("Otra definición", strategy["definition"])
    with pytest.raises(LabError, match="otra versión"):
        paper.create(unrelated["version_id"], dataset["id"], origin["id"])
    with lab.connect() as db:
        db.execute("DELETE FROM detected_events WHERE run_id=?", (origin["id"],))
        db.execute("DELETE FROM simulated_trades WHERE run_id=?", (origin["id"],))
        db.execute("DELETE FROM runs WHERE id=?", (origin["id"],))
    comparison = paper.comparison(session["id"])
    assert comparison["historical"]["run_id"] == origin["id"]
    assert comparison["historical"]["verification"]["verified"] is False
    assert comparison["historical"]["metrics"] is None


def test_spot_paper_comparison_detects_tampered_observation_without_rewriting_it(tmp_path):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    assert paper.comparison(session["id"])["signal_replay"]["verified"] is True
    with paper.connect() as db:
        row = db.execute("SELECT payload_json FROM events WHERE session_id=? AND kind='OBSERVATION'",
                         (session["id"],)).fetchone()
        altered = json.loads(row["payload_json"])
        altered["entry_match"] = not altered["entry_match"]
        db.execute("UPDATE events SET payload_json=? WHERE session_id=? AND kind='OBSERVATION'",
                   (json.dumps(altered), session["id"]))
    check = paper.comparison(session["id"])["signal_replay"]
    assert check["status"] == "mismatch" and check["first_mismatch_t"] is not None
    assert check["checked_observations"] == 0
    assert paper.comparison(session["id"])["signal_frequency"]["paper"] is None


def test_spot_comparison_rejects_secondary_segmented_replay_divergence(tmp_path, monkeypatch):
    import app.automation.spot_strategy_paper as paper_module

    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    assert paper.comparison(session["id"])["signal_replay"]["verified"] is True
    monkeypatch.setattr(paper_module, "verify_archived_paper_signals", lambda *args, **kwargs: {
        "verified": False, "status": "mismatch", "reason": "discrepancia controlada",
        "checked_observations": 0, "first_mismatch_t": START + 3 * STEP})
    report = paper.comparison(session["id"])["signal_replay"]
    assert report["status"] == "mismatch" and "segmentado" in report["reason"]
    assert report["first_mismatch_t"] == START + 3 * STEP


def test_spot_paper_comparison_does_not_trust_tampered_lab_run(tmp_path):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    bars, _ = validate_candles(state["rows"][:3], "1h", now=state["now"])
    result = run_backtest(bars, strategy["definition"], "1h")
    run = lab.save_run(dataset["id"], strategy["version_id"], result, CURRENT_ENGINE_VERSION)
    with lab.connect() as db:
        saved = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (run["id"],)).fetchone()[0])
        saved["metrics"]["net_profit"] = "999999"
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(saved), run["id"]))
    compared = paper.comparison(session["id"])
    assert compared["historical"]["verification"]["verified"] is False
    assert compared["historical"]["metrics"] is None


def test_paper_entry_exports_only_fresh_reproducible_signal_not_fake_order(tmp_path):
    paper, state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    now = lambda: datetime.fromtimestamp(state["now"], timezone.utc)
    with pytest.raises(SpotPaperSignalError, match="última vela"):
        current_spot_paper_entry_signal(paper, session["id"], now=now(), max_age_seconds=60)
    state["now"] = START + 4 * STEP + 1
    assert paper.poll(session["id"])["events"][0]["action"]["side"] == "BUY"
    signal = current_spot_paper_entry_signal(paper, session["id"], now=now(), max_age_seconds=60)
    assert signal.signal_id == current_spot_paper_entry_signal(
        paper, session["id"], now=now(), max_age_seconds=60).signal_id
    assert signal.strategy_version_id == strategy["version_id"]
    assert signal.dataset_hash == dataset["hash"]
    assert signal.candle_t == START + 3 * STEP
    assert signal.signal_at == datetime.fromtimestamp(START + 4 * STEP, timezone.utc)
    assert signal.entry_evidence["matched"] is True
    assert signal.paper_entry_status == "EXECUTED_PAPER"
    assert not hasattr(signal, "quantity") and not hasattr(signal, "price")
    state["now"] += 61
    with pytest.raises(SpotPaperSignalError, match="caducada"):
        current_spot_paper_entry_signal(paper, session["id"], now=now(), max_age_seconds=60)


def test_live_signal_rejects_corrupt_fictitious_paper_cash(tmp_path):
    paper, state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("UPDATE sessions SET state_json=json_set(state_json,'$.cash','0') WHERE id=?",
                   (session["id"],))
    state["now"] = START + 4 * STEP + 1
    result = paper.poll(session["id"])
    assert result["status"] == "GAP" and result["events"] == []
    with pytest.raises(SpotPaperSignalError):
        current_spot_paper_entry_signal(
            paper, session["id"], now=datetime.fromtimestamp(state["now"], timezone.utc),
            max_age_seconds=60)


def test_paper_signal_builds_deterministic_real_intent_with_independent_sizing(tmp_path):
    paper, state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    now = datetime.fromtimestamp(state["now"], timezone.utc)
    signal = current_spot_paper_entry_signal(paper, session["id"], now=now,
                                             max_age_seconds=60)
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id=strategy["version_id"],
        account_ref="a" * 64, symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("100"), max_daily_turnover_quote=Decimal("200"),
        max_exposure_quote=Decimal("300"), max_daily_loss_quote=Decimal("20"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=now + timedelta(hours=1),
    )
    args = {"quantity_base": Decimal("0.001"), "limit_price_quote": Decimal("100000"),
            "estimated_fee_quote": Decimal("0.1"), "now": now}
    intent = live_buy_intent_from_signal(signal, policy, **args)
    assert intent.intent_id != signal.signal_id
    assert intent.side == "BUY" and intent.signal_at == signal.signal_at
    assert intent.quantity_base == Decimal("0.001")
    assert intent.limit_price_quote == Decimal("100000")
    assert live_buy_intent_from_signal(signal, policy, **args) == intent
    assert live_buy_intent_from_signal(
        replace(signal, paper_session_id=str(uuid4()), signal_id=str(uuid4())),
        policy, **args).intent_id == intent.intent_id
    with pytest.raises(SpotPaperSignalError, match="no coinciden"):
        live_buy_intent_from_signal(signal, replace(policy, strategy_version_id="otra"), **args)
    with pytest.raises(SpotPaperSignalError, match="caducaron"):
        live_buy_intent_from_signal(signal, policy, **{**args, "now": now + timedelta(seconds=61)})


def test_paper_signal_rejects_tampering_and_stopped_session(tmp_path):
    paper, state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    now = datetime.fromtimestamp(state["now"], timezone.utc)
    with paper.connect() as db:
        db.execute("""UPDATE events SET payload_json=json_set(payload_json,'$.entry.observed','999')
                      WHERE session_id=? AND kind='OBSERVATION'""", (session["id"],))
    with pytest.raises(SpotPaperSignalError, match="no reproduce"):
        current_spot_paper_entry_signal(paper, session["id"], now=now, max_age_seconds=60)
    paper.stop(session["id"])
    with pytest.raises(SpotPaperSignalError, match="no activa"):
        current_spot_paper_entry_signal(paper, session["id"], now=now, max_age_seconds=60)


def test_paper_signal_rejects_entry_when_paper_position_was_already_open(tmp_path):
    paper, state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    now = datetime.fromtimestamp(state["now"], timezone.utc)
    with paper.connect() as db:
        db.execute("""UPDATE events SET payload_json=json_set(
            payload_json, '$.entry_status', 'SKIPPED_POSITION_OPEN', '$.action', NULL)
            WHERE session_id=? AND kind='OBSERVATION'""", (session["id"],))
    with pytest.raises(SpotPaperSignalError, match="ausente"):
        current_spot_paper_entry_signal(paper, session["id"], now=now,
                                        max_age_seconds=60)


def test_current_paper_signal_reserves_real_terms_once_and_stopped_paper_cannot_reenter(tmp_path):
    paper, state, _lab, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    now = datetime.fromtimestamp(state["now"], timezone.utc)
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("isolated paper bridge test password", create=True)
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id=strategy["version_id"],
        account_ref="a" * 64, symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=now + timedelta(hours=1), enabled=True,
    )
    register_spot_policy(vault, policy, now=now)
    observed = LiveSpotState(
        account_ref=policy.account_ref, symbol=policy.symbol,
        reconciled=True, stopped=False, account_verified=True,
        instrument_verified=True, observed_at=now, quote_at=now,
        best_bid=Decimal("99"), best_ask=Decimal("100"),
        free_base=Decimal("0"), free_quote=Decimal("200"),
        pending_base=Decimal("0"), pending_quote=Decimal("0"),
        daily_turnover_quote=Decimal("0"), pending_turnover_quote=Decimal("0"),
        exposure_quote=Decimal("0"), daily_realized_pnl_quote=Decimal("0"),
    )
    terms = {"quantity_base": Decimal("1"), "limit_price_quote": Decimal("100"),
             "estimated_fee_quote": Decimal("0.1"), "now": now}
    first = reserve_current_paper_buy(vault, paper, session["id"], policy, observed, **terms)
    assert first.reservation.status == "RESERVED"
    assert first.reservation.reserved_quote == Decimal("100.1")
    assert first.intent.quantity_base == Decimal("1")
    assert first.source.dataset_hash == dataset["hash"]
    assert recover_spot_signal_source(vault, first.intent.intent_id) == first.source
    origin = list_spot_execution_intents(vault)[0]["origin"]
    assert origin["status"] == "ARCHIVED_PAPER"
    assert origin["strategy_version_id"] == strategy["version_id"]
    assert origin["dataset_hash"] == dataset["hash"]
    assert origin["signal_at"] == first.signal.signal_at.isoformat()
    with pytest.raises(LiveJournalError, match="source differs"):
        reserve_spot_intent(
            vault, first.intent, observed, now=now,
            signal_source=replace(first.source, dataset_id=str(uuid4())),
        )
    assert reserve_current_paper_buy(
        vault, paper, session["id"], policy, observed, **terms).intent.intent_id == first.intent.intent_id
    with pytest.raises(LiveJournalError, match="Intent ID was reused"):
        reserve_current_paper_buy(
            vault, paper, session["id"], policy, observed,
            **{**terms, "quantity_base": Decimal("0.5")})

    class View:
        def account_reference(self):
            return policy.account_ref

        def account_uid(self):
            return "123456789"

    class Trade:
        def __init__(self):
            self.tests = 0
            self.posts = 0

        def pair_view_account(self, uid):
            assert uid == "123456789"

        def test_reserved_limit(self, intent, recorded, preflight):
            self.tests += 1

        def place_limit_order(self, intent):
            self.posts += 1
            return MexcOrderAcknowledgement(intent.intent_id, intent.intent_id.replace("-", ""),
                                            "fixture-order")

    rules = MexcSpotRules(
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", observed_at=now,
        buy_allowed=True, sell_allowed=True, base_precision=6, quote_precision=2,
        base_step=Decimal("0.000001"), min_quote=Decimal("1"),
        max_quote=Decimal("1000"), fee_rate_upper=Decimal("0.0005"),
        bid_multiplier_up=None, ask_multiplier_down=None,
    )
    trade = Trade()
    refreshed_at = now + timedelta(seconds=1)
    send = lambda **kwargs: submit_spot_limit_once(
        vault, trade, View(), policy, first.intent, observed, rules,
        last_price=Decimal("100"), last_price_at=now, clock=lambda: refreshed_at,
        refresh_inputs=lambda: MexcLiveInputs(
            replace(observed, observed_at=refreshed_at, quote_at=refreshed_at),
            replace(rules, observed_at=refreshed_at), Decimal("100"), refreshed_at),
        **kwargs,
    )
    with pytest.raises(MexcOrderSendError, match="exige revalidación"):
        send()
    assert trade.tests == 0 and trade.posts == 0
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT COUNT(*) FROM real_mexc_uid_links").scalar_one() == 0
    with paper.connect() as db:
        original_payload = db.execute(
            "SELECT payload_json FROM events WHERE session_id=? AND kind='OBSERVATION'",
            (session["id"],),
        ).fetchone()[0]
        db.execute("""UPDATE events SET payload_json=json_set(payload_json,'$.quote.bid','98')
                      WHERE session_id=? AND kind='OBSERVATION'""", (session["id"],))
    with pytest.raises(MexcOrderSendError, match="Señal PAPER no verificable"):
        send(paper=paper)
    assert trade.tests == 0 and trade.posts == 0
    with paper.connect() as db:
        db.execute("UPDATE events SET payload_json=? WHERE session_id=? AND kind='OBSERVATION'",
                   (original_payload, session["id"]))
        db.execute("""UPDATE events SET payload_json=json_set(payload_json,'$.entry.observed','999')
                      WHERE session_id=? AND kind='OBSERVATION'""", (session["id"],))
    with pytest.raises(MexcOrderSendError, match="Señal PAPER no verificable"):
        send(paper=paper)
    assert trade.posts == 0
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status FROM real_live_spot_intents WHERE id=?",
            (first.intent.intent_id,),
        ).scalar_one() == "RESERVED"
    with paper.connect() as db:
        db.execute("UPDATE events SET payload_json=? WHERE session_id=? AND kind='OBSERVATION'",
                   (original_payload, session["id"]))
    acknowledgement = send(paper=paper)
    assert acknowledgement.intent_id == first.intent.intent_id
    assert trade.tests == 1 and trade.posts == 1
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT signal_source_json,signal_source_digest FROM real_live_spot_intents WHERE id=?",
            (first.intent.intent_id,),
        ).fetchone()
        archived_json, archived_digest = row
        legacy = json.loads(archived_json)
        legacy.pop("observation_digest")
        legacy_json = json.dumps(legacy, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        db.connection().exec_driver_sql(
            "UPDATE real_live_spot_intents SET signal_source_json=?,signal_source_digest=? WHERE id=?",
            (legacy_json, hashlib.sha256(legacy_json.encode()).hexdigest(), first.intent.intent_id),
        )
        db.commit()
    assert recover_spot_signal_source(vault, first.intent.intent_id).observation_digest is None
    assert list_spot_execution_intents(vault)[0]["origin"]["status"] == "LEGACY_PAPER_UNPINNED"
    with pytest.raises(SpotPaperSignalError, match="no fijó la observación"):
        with verified_archived_paper_buy_source(vault, paper, policy, first.intent, now=now):
            pass
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE real_live_spot_intents SET signal_source_json=?,signal_source_digest=? WHERE id=?",
            (archived_json, archived_digest, first.intent.intent_id),
        )
        db.commit()
    paper.stop(session["id"])
    with pytest.raises(SpotPaperSignalError, match="no activa"):
        with verified_archived_paper_buy_source(
                vault, paper, policy, first.intent, now=now):
            pass
    with pytest.raises(SpotPaperSignalError, match="no activa"):
        reserve_current_paper_buy(vault, paper, session["id"], policy, observed, **terms)
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT COUNT(*) FROM real_live_spot_intents").scalar_one() == 1
    vault.lock()
    vault.unlock("isolated paper bridge test password")
    assert recover_spot_signal_source(vault, first.intent.intent_id) == first.source
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE real_live_spot_intents SET signal_source_json='{}' WHERE id=?",
            (first.intent.intent_id,),
        )
        db.commit()
    with pytest.raises(LiveJournalError, match="integrity"):
        recover_spot_signal_source(vault, first.intent.intent_id)
    assert list_spot_execution_intents(vault)[0]["origin"] == {"status": "INVALID_SOURCE"}
    vault.lock()


def test_spot_paper_time_limit_closes_after_observed_candle(tmp_path):
    paper, state, lab, dataset, _strategy = harness(tmp_path)
    limited = lab.create_strategy("Permanencia LAB", validate_strategy({
        **definition(), "max_holding_bars": 1}))
    session = paper.create(limited["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    first = paper.poll(session["id"])
    assert first["events"][0]["action"]["side"] == "BUY"
    assert first["state"]["entry_signal_t"] == START + 3 * STEP
    keep_risk_covered(paper, state, session["id"], START + 5 * STEP + 1)
    state["rows"][4] = {**state["rows"][4], "o": "100", "h": "101", "l": "98", "c": "99"}
    state["now"] = START + 5 * STEP + 1
    second = paper.poll(session["id"])
    assert second["poll_result"] == "OBSERVED"
    assert second["events"][1]["action"]["reason"] == "TIME_LIMIT"
    assert second["state"]["units"] == "0"
    assert second["state"]["entry_signal_t"] is None
    assert len(paper.chart(session["id"])["markers"]) == 5
    restored = SpotStrategyPaper(paper.path, paper.lab.path)
    assert restored.detail(session["id"])["state"] == second["state"]


def test_spot_paper_time_limit_never_reconstructs_a_missed_candle(tmp_path):
    paper, state, lab, dataset, _strategy = harness(tmp_path)
    limited = lab.create_strategy("Permanencia LAB", {
        **definition(), "max_holding_bars": 1})
    session = paper.create(limited["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    state["now"] = START + 5 * STEP + 1
    result = paper.poll(session["id"])
    assert result["poll_result"] == "GAP"
    assert result["state"]["units"] != "0"
    assert not any(event.get("action", {}).get("reason") == "TIME_LIMIT"
                   for event in result["events"] if isinstance(event.get("action"), dict))


def test_spot_paper_time_limit_missing_entry_clock_fails_closed(tmp_path):
    paper, state, lab, dataset, _strategy = harness(tmp_path)
    limited = lab.create_strategy("Permanencia LAB", {
        **definition(), "max_holding_bars": 1})
    session = paper.create(limited["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    with paper.connect() as db:
        db.execute("UPDATE sessions SET state_json=json_remove(state_json,'$.entry_signal_t') WHERE id=?",
                   (session["id"],))
    assert paper.poll(session["id"])["poll_result"] == "GAP"


def test_spot_paper_ema_cross_uses_same_closed_bar_rule_as_lab(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    ema_rule = {"kind": "EMA_CROSS_ABOVE_EMA", "period": 2, "slow_period": 3}
    strategy = lab.create_strategy("EMA Spot", {**definition(), "entry": ema_rule})
    state["rows"][3] = {**state["rows"][3], "o": "100", "h": "112", "l": "96", "c": "110"}
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    bars, gaps = validate_candles(state["rows"][:4], "1h", now=state["now"])
    assert gaps == []
    expected = evaluate(validate_strategy(strategy["definition"])["entry"],
                        bars, 3, IndicatorCache(bars))
    assert observed["events"][0]["entry_match"] == expected[0] is True
    assert observed["events"][0]["entry"] == expected[1]
    assert observed["events"][0]["action"]["side"] == "BUY"
    assert observed["execution"].endswith("NO_REAL_ORDERS")


def keep_risk_covered(paper, state, session_id, until):
    """Reloj ficticio con observaciones intermedias; no salta una hora sin datos."""
    while state["now"] + 25 < until:
        state["now"] += 25
        assert paper.poll(session_id)["poll_result"] == "WAIT"


def test_spot_paper_open_valuation_expires_without_erasing_last_observation(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    assert opened["state"]["units"] != "0"
    assert opened["valuation"]["basis"] == "ESTIMATED_POST_CLOSE_BID_COSTS_NOT_EXECUTED"
    last_quote = opened["last_quote"]

    state["now"] += 15
    assert paper.detail(session["id"])["valuation"]["basis"] == "ESTIMATED_POST_CLOSE_BID_COSTS_NOT_EXECUTED"
    state["now"] += 1
    stale = paper.detail(session["id"])
    assert stale["last_quote"] == last_quote
    assert stale["valuation"] == {
        "basis": "UNAVAILABLE_STALE_QUOTE", "estimated_exit_price": None,
        "estimated_exit_fee": None, "estimated_proceeds": None,
        "equity_if_closed": None, "unrealized_pnl_if_closed": None,
        "total_pnl_if_closed": None,
        "quote_event_time_ns": last_quote["event_time_ns"],
    }
    assert paper.sessions()[0]["valuation"]["basis"] == "UNAVAILABLE_STALE_QUOTE"
    assert paper.poll(session["id"])["poll_result"] == "WAIT"
    refreshed = paper.detail(session["id"])
    assert refreshed["valuation"]["basis"] == "ESTIMATED_POST_CLOSE_BID_COSTS_NOT_EXECUTED"
    assert refreshed["last_quote"]["event_time_ns"] > last_quote["event_time_ns"]


def test_spot_paper_buy_sell_exact_and_persistent(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    with pytest.raises(SpotStrategyError, match="ya tienen una sesión"):
        paper.create(strategy["version_id"], dataset["id"])
    assert session["execution"].endswith("NO_REAL_ORDERS")
    assert session["state"]["cash"] == "1000"
    assert paper.poll(session["id"])["poll_result"] == "WAIT"

    state["now"] = START + 4 * STEP + 1
    first = paper.poll(session["id"])
    assert first["poll_result"] == "OBSERVED"
    assert first["events"][0]["entry_match"] is True
    assert first["events"][0]["action"]["side"] == "BUY"
    assert Decimal(first["state"]["cash"]) == 900
    assert first["valuation"]["basis"] == "ESTIMATED_POST_CLOSE_BID_COSTS_NOT_EXECUTED"
    with localcontext() as context:
        context.prec = 80
        units = Decimal(100) / Decimal("100.1")
        proceeds = units * Decimal(99) * Decimal("0.999")
        assert Decimal(first["valuation"]["estimated_proceeds"]) == proceeds
        assert Decimal(first["valuation"]["equity_if_closed"]) == Decimal(900) + proceeds
        assert Decimal(first["valuation"]["unrealized_pnl_if_closed"]) == proceeds - Decimal(100)
    first_chart = paper.chart(session["id"])
    assert [mark["kind"] for mark in first_chart["markers"]] == ["START", "SIGNAL", "BUY"]
    assert first_chart["markers"][1]["time"] == START + 3 * STEP
    assert first_chart["markers"][2]["time"] == START + 4 * STEP
    assert first_chart["markers"][2]["pending_candle"] is True
    assert first_chart["markers"][2]["observed_at_ns"] > (START + 4 * STEP) * 1_000_000_000
    assert first_chart["execution"] == "PAPER_ACTION_AFTER_SIGNAL_CANDLE_CLOSE_NO_REAL_ORDER"
    with localcontext() as context:
        context.prec = 80
        assert Decimal(first["state"]["units"]) == Decimal(100) / Decimal("100.1")
    assert paper.poll(session["id"])["poll_result"] == "WAIT"
    assert len(paper.detail(session["id"])["events"]) == 1

    keep_risk_covered(paper, state, session["id"], START + 5 * STEP + 1)
    state["now"] = START + 5 * STEP + 1
    state["bid"], state["ask"] = "110", "111"
    second = paper.poll(session["id"])
    assert second["poll_result"] == "OBSERVED"
    assert second["events"][1]["action"]["side"] == "SELL"
    assert second["events"][1]["action"]["reason"] == "TARGET_CLOSE"
    assert second["state"]["units"] == "0"
    assert second["state"]["closed_trades"] == 1
    assert second["valuation"]["basis"] == "CASH_ONLY"
    assert Decimal(second["valuation"]["equity_if_closed"]) == Decimal(second["state"]["cash"])
    assert second["valuation"]["unrealized_pnl_if_closed"] == "0"
    assert [mark["kind"] for mark in paper.chart(session["id"])["markers"]] == [
        "START", "SIGNAL", "BUY", "SIGNAL_SKIPPED", "SELL"]
    assert second["events"][1]["entry_status"] == "SKIPPED_POSITION_OPEN"
    with localcontext() as context:
        context.prec = 80
        gross = Decimal(first["state"]["units"]) * Decimal(110)
        expected_pnl = gross * Decimal("0.999") - Decimal(100)
        assert Decimal(second["state"]["realized_pnl"]) == expected_pnl
    restored = SpotStrategyPaper(paper.path, paper.lab.path)
    assert restored.detail(session["id"])["state"] == second["state"]
    assert len(restored.detail(session["id"])["events"]) == 2
    assert paper.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("engine_version", ["lab-1.5", "lab-1.6"])
def test_spot_paper_legacy_session_continues_with_unchanged_rules(tmp_path, engine_version):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    # Una sesión anterior a v4 no tenía aún instantánea incremental.
    with sqlite3.connect(paper.path) as db:
        db.execute("DROP TABLE session_candles")
        db.execute("UPDATE sessions SET engine_version=? WHERE id=?", (engine_version, session["id"]))
        db.execute("ALTER TABLE sessions DROP COLUMN indicator_state_json")
        db.execute("PRAGMA user_version=3")
    state["now"] = START + 4 * STEP + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    assert observed["status"] == "ACTIVE"
    assert observed["events"][0]["action"]["side"] == "BUY"


@pytest.mark.parametrize("bid,reason", [
    ("96", "STOP_OBSERVED_BID"), ("109", "TARGET_OBSERVED_BID"),
])
def test_spot_paper_intrabar_risk_uses_observed_bid_and_marks_closed_bar(
        tmp_path, bid, reason):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    assert opened["events"][0]["action"]["side"] == "BUY"
    assert paper.scheduled_sessions() == [{"id": session["id"], "due_at": state["now"] + 5}]

    state["now"] += 5
    state["bid"] = bid
    state["ask"] = str(Decimal(bid) + 1)
    exit_result = paper.poll(session["id"])
    assert exit_result["poll_result"] == "RISK_EXIT"
    assert exit_result["state"]["units"] == "0"
    assert exit_result["state"]["closed_trades"] == 1
    event = exit_result["events"][-1]
    assert event["kind"] == "INTRABAR_EXIT"
    assert event["action"]["reason"] == reason
    assert Decimal(event["action"]["price"]) == Decimal(bid)
    with localcontext() as context:
        context.prec = 80
        assert Decimal(event["action"]["pnl"]) == (Decimal(opened["state"]["units"]) * Decimal(bid) *
                                                      Decimal("0.999") - Decimal("100"))
    intrabar_chart = paper.chart(session["id"])
    assert [mark["kind"] for mark in intrabar_chart["markers"]][-1] == "SELL"
    assert intrabar_chart["markers"][-1]["time"] == START + 4 * STEP
    assert intrabar_chart["markers"][-1]["pending_candle"] is True
    assert paper.scheduled_sessions() == [{"id": session["id"], "due_at": START + 5 * STEP}]
    assert paper.poll(session["id"])["poll_result"] == "WAIT"
    assert len(paper.detail(session["id"])["events"]) == 2
    state["now"] = START + 5 * STEP + 1
    after_close = paper.poll(session["id"])
    assert after_close["poll_result"] == "OBSERVED"
    marked = [mark for mark in paper.chart(session["id"])["markers"]
              if mark.get("reason") == reason]
    assert len(marked) == 1 and marked[0]["time"] == START + 4 * STEP
    restored = SpotStrategyPaper(paper.path, paper.lab.path)
    assert restored.detail(session["id"])["state"] == after_close["state"]


def test_spot_paper_intrabar_double_poll_and_unavailable_quote_fail_closed(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    state["now"] += 5
    state["offline"] = True
    with pytest.raises(Exception, match="cotización|Ticker|MEXC"):
        paper.poll(session["id"])
    assert paper.detail(session["id"])["state"]["units"] != "0"
    state["offline"] = False
    state["bid"] = "96"
    state["ask"] = "97"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: paper.poll(session["id"])["poll_result"], range(2)))
    assert outcomes.count("RISK_EXIT") == 1
    assert paper.detail(session["id"])["state"]["closed_trades"] == 1
    assert [item["kind"] for item in paper.detail(session["id"])["events"]].count("INTRABAR_EXIT") == 1


def test_spot_paper_intrabar_check_without_trigger_reschedules_and_event_failure_rolls_back(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    state["now"] += 5
    no_hit = paper.poll(session["id"])
    assert no_hit["poll_result"] == "WAIT"
    assert no_hit["state"]["units"] != "0"
    assert no_hit["last_quote"]["checked_at_ns"] == state["now"] * 1_000_000_000
    assert paper.scheduled_sessions() == [{"id": session["id"], "due_at": state["now"] + 5}]
    state["now"] += 5
    state["bid"], state["ask"] = "96", "97"
    with paper.connect() as db:
        db.execute("""CREATE TRIGGER fail_risk_exit BEFORE INSERT ON events
                      WHEN NEW.kind='INTRABAR_EXIT'
                      BEGIN SELECT RAISE(ABORT,'risk event failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="risk event failure"):
        paper.poll(session["id"])
    unchanged = paper.detail(session["id"])
    assert unchanged["state"] == no_hit["state"]
    assert unchanged["last_quote"] == no_hit["last_quote"]
    assert len(unchanged["events"]) == 1


def test_spot_paper_risk_coverage_gap_does_not_invent_stop_or_resume(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    state["now"] += 31
    state["bid"], state["ask"] = "96", "97"
    gap = paper.poll(session["id"])
    assert gap["poll_result"] == "GAP"
    assert gap["state"] == opened["state"]
    assert gap["status_reason"].startswith("Stop/objetivo sin observación")
    assert gap["events"][-1]["kind"] == "RISK_COVERAGE_GAP"
    assert gap["events"][-1]["unobserved_seconds"] == 31
    assert gap["events"][-1]["action"] is None
    assert paper.poll(session["id"])["poll_result"] == "GAP"
    assert len(paper.detail(session["id"])["events"]) == 2
    restored = SpotStrategyPaper(paper.path, paper.lab.path)
    assert restored.detail(session["id"])["status"] == "GAP"


def test_spot_paper_fails_closed_on_gap_revision_and_wrong_strategy(tmp_path):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    short = lab.create_strategy("Short no spot", definition("SHORT"))
    with pytest.raises(SpotStrategyError, match="no permite SHORT"):
        paper.create(short["version_id"], dataset["id"])
    session = paper.create(strategy["version_id"], dataset["id"])
    state["revised"] = True
    state["now"] = START + 4 * STEP + 1
    revised = paper.poll(session["id"])
    assert revised["poll_result"] == "GAP"
    assert revised["events"] == []
    assert revised["state"]["cash"] == "1000"
    assert paper.poll(session["id"])["poll_result"] == "GAP"

    state["revised"] = False
    state["now"] = START + 3 * STEP + 10
    second = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 901
    missed = paper.poll(second["id"])
    assert missed["poll_result"] == "GAP"
    assert missed["events"] == []


def test_spot_paper_gap_leaves_position_and_manual_close_is_idempotent(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    assert paper.poll(session["id"])["state"]["units"] != "0"
    state["now"] = START + 5 * STEP + 901
    gap = paper.poll(session["id"])
    assert gap["poll_result"] == "GAP"
    assert gap["state"]["units"] != "0"
    state["bid"], state["ask"] = "90", "91"
    closed = paper.close_position(session["id"])
    assert closed["close_result"] == "CLOSED"
    assert closed["status"] == "GAP"
    assert closed["state"]["units"] == "0"
    assert Decimal(closed["valuation"]["equity_if_closed"]) == Decimal(closed["state"]["cash"])
    assert closed["events"][-1]["action"]["reason"] == "MANUAL_CLOSE"
    chart = paper.chart(session["id"])
    assert [mark["kind"] for mark in chart["markers"]] == ["START", "SIGNAL", "BUY", "SELL"]
    assert chart["markers"][-1]["reason"] == "MANUAL_CLOSE"
    assert chart["markers"][-1]["time"] == START + 5 * STEP
    assert chart["markers"][-1]["pending_candle"] is True
    repeated = paper.close_position(session["id"])
    assert repeated["close_result"] == "NO_POSITION"
    assert len(repeated["events"]) == 3


def test_spot_paper_estimated_liquidation_matches_manual_close_with_costs(tmp_path):
    paper, state, lab, dataset, _ = harness(tmp_path)
    configured = definition() | {"fee_rate": "0.01", "slippage_rate": "0.02"}
    strategy = lab.create_strategy("Costes spot", configured)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    opened = paper.poll(session["id"])
    estimate = opened["valuation"]
    assert estimate["basis"] == "ESTIMATED_POST_CLOSE_BID_COSTS_NOT_EXECUTED"
    assert Decimal(estimate["estimated_exit_price"]) == Decimal("99") * Decimal("0.98")
    closed = paper.close_position(session["id"])
    assert closed["close_result"] == "CLOSED"
    action = closed["events"][-1]["action"]
    assert Decimal(estimate["estimated_exit_fee"]) == Decimal(action["fee"])
    assert Decimal(estimate["estimated_proceeds"]) == Decimal(action["cash_change"])
    assert Decimal(estimate["unrealized_pnl_if_closed"]) == Decimal(action["pnl"])
    assert Decimal(estimate["equity_if_closed"]) == Decimal(closed["state"]["cash"])


def test_spot_paper_api_requires_local_active_observer(tmp_path):
    paper, _, _, dataset, strategy = harness(tmp_path)
    with paper.connect():  # Sesión heredada inicializada para aislar la precondición del observador.
        pass
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    client = TestClient(app)
    body = {"dataset_id": dataset["id"], "version_id": strategy["version_id"]}
    assert client.post("/paper/spot/strategies", json=body).status_code == 503
    assert client.get("/paper/spot/strategies", headers={"Origin": "https://outside.invalid"}).status_code == 403

    class Worker:
        def snapshot(self):
            return {"running": True}

        def wake(self):
            pass

    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path,
                                                   paper=paper, worker=Worker()))
    client = TestClient(app)
    created = client.post("/paper/spot/strategies", json=body)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert client.get(f"/paper/spot/strategies/{session_id}").status_code == 200
    assert client.get(f"/paper/spot/strategies/{session_id}/chart").json()["markers"][0]["kind"] == "START"
    comparison = client.get(f"/paper/spot/strategies/{session_id}/comparison")
    assert comparison.status_code == 200 and comparison.json()["historical"] is None
    assert client.get(f"/paper/spot/strategies/{session_id}/comparison",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403
    assert client.post(f"/paper/spot/strategies/{session_id}/stop").json()["status"] == "STOPPED"
    assert client.get("/paper/spot/strategies/missing").status_code == 404


def test_spot_paper_concurrent_poll_records_one_purchase(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: paper.poll(session["id"])["poll_result"], range(2)))
    assert outcomes.count("OBSERVED") == 1
    assert set(outcomes) <= {"OBSERVED", "CHANGED", "WAIT"}
    detail = paper.detail(session["id"])
    assert len(detail["events"]) == 1
    assert detail["events"][0]["action"]["side"] == "BUY"
    assert Decimal(detail["state"]["cash"]) == 900


def test_spot_paper_observation_and_balance_roll_back_together(tmp_path):
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], dataset["id"])
    state["now"] = START + 4 * STEP + 1
    with paper.connect() as db:
        db.execute("""CREATE TRIGGER fail_spot_event BEFORE INSERT ON events
                      BEGIN SELECT RAISE(ABORT,'test event failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test event failure"):
        paper.poll(session["id"])
    unchanged = paper.detail(session["id"])
    assert unchanged["events"] == []
    assert unchanged["state"]["cash"] == "1000"
    assert unchanged["last_t"] == START + 2 * STEP
    with paper.connect() as db:
        db.execute("DROP TRIGGER fail_spot_event")
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"


def make_spot_reserve(lab, strategy, *, end=START-STEP, rolling=False, price="100"):
    from app.lab.validation import (ROLLING_ENGINE_VERSION, VALIDATION_ENGINE_VERSION,
                                    chronological_holdout, rolling_fixed_validation)

    raw = [{"t":end-(119-index)*STEP,"o":"101","h":"102","l":"98","c":price,"v":"20"}
           for index in range(120)]
    bars, gaps = validate_candles(raw, "1h", now=end+STEP)
    data = lab.save_dataset("BTC/USDT","1h","mexc-spot-public",bars,gaps,verified_public=True)
    result = (rolling_fixed_validation(bars,strategy["definition"],"1h",3)
              if rolling else chronological_holdout(bars,strategy["definition"],"1h","0.5"))
    engine = ROLLING_ENGINE_VERSION if rolling else VALIDATION_ENGINE_VERSION
    return lab.save_validation(data["id"],strategy["version_id"],data["hash"],result,engine,
                               lab_engine_version=CURRENT_ENGINE_VERSION)


@pytest.mark.parametrize("rolling",[False,True],ids=["holdout","rolling"])
def test_spot_reserve_comparison_replays_and_preserves_separate_exact_periods(tmp_path, rolling):
    from app.automation.spot_reserve_comparison import compare_spot_reserve
    from app.lab.store import content_hash

    paper, state, lab, seed, strategy = harness(tmp_path)
    reserve = make_spot_reserve(lab,strategy,rolling=rolling)
    session = paper.create(strategy["version_id"],seed["id"])
    args = (paper,session["id"],reserve["id"])
    empty = compare_spot_reserve(*args)
    assert empty["paper"]["observed_candles"] == 0 and empty["paper"]["metrics"]["closed_trades"] == 0
    state["now"] = START+4*STEP+1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    archived_open = compare_spot_reserve(*args)
    assert archived_open["paper"]["position_open"]
    assert archived_open["paper"]["metrics"]["return_pct"] is None
    assert archived_open["paper"]["metrics"]["equity"] is None
    assert Decimal(archived_open["paper"]["metrics"]["fees_paid"]) > 0
    state["now"] += 1
    closed = paper.close_position(session["id"])
    assert closed["close_result"] == "CLOSED"
    calls = state["quote_calls"],state["candle_calls"]
    report = compare_spot_reserve(*args)
    assert report["paper"]["event_count"] == 2 and report["paper"]["observed_candles"] == 1
    assert report["paper"]["metrics"]["closed_trades"] == 1
    assert report["paper"]["through_ns"] == str(state["now"]*10**9)
    assert report["paper"]["started_at_ns"] == str(session["started_at_ns"])
    assert report["execution_authority"] == "NONE"
    assert report["comparison_basis"] == "DISTINCT_WINDOWS_AND_EXECUTION"
    assert report["overlap_bars"] == 0 and "delta" not in report
    assert report["reference"]["artifact_id"] == reserve["id"]
    windows = reserve["windows"] if rolling else [reserve]
    for saved, checked in zip(windows,report["reserves"],strict=True):
        assert checked["metrics"]["equity"] == saved["out_of_sample"]["metrics"]["final_equity"]
        assert checked["from_t"] == saved["test_start_t"] and checked["end_t"] == saved["test_end_t"]+STEP
    with localcontext() as context:
        context.prec = 80
        fees = sum(Decimal(event["action"]["fee"]) for event in closed["events"] if event["action"])
        assert Decimal(report["paper"]["metrics"]["fees_paid"]) == fees
        assert Decimal(report["paper"]["metrics"]["realized_pnl"]) == Decimal(closed["state"]["realized_pnl"])
        assert Decimal(report["paper"]["metrics"]["return_pct"]) == Decimal(closed["state"]["realized_pnl"])/10
    assert report["hash"] == content_hash({key:value for key,value in report.items() if key!="hash"})
    reopened = SpotStrategyPaper(paper.path, paper.lab.path, allow_create=False)
    assert compare_spot_reserve(reopened,*args[1:]) == report
    assert (state["quote_calls"],state["candle_calls"]) == calls


def test_spot_reserve_comparison_rejects_wrong_version_future_bars_and_overlapping_changes(tmp_path):
    from app.automation.spot_reserve_comparison import compare_spot_reserve

    paper, _, lab, seed, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"],seed["id"])
    conflicting = make_spot_reserve(lab,strategy,end=START+STEP)
    with pytest.raises(LabError,match="velas comunes"):
        compare_spot_reserve(paper,session["id"],conflicting["id"])
    future = make_spot_reserve(lab,strategy,end=START+3*STEP)
    with pytest.raises(LabError,match="anteriores"):
        compare_spot_reserve(paper,session["id"],future["id"])
    other = lab.create_strategy("Another version",definition())
    wrong_version = make_spot_reserve(lab,other)
    with pytest.raises(LabError,match="misma versión"):
        compare_spot_reserve(paper,session["id"],wrong_version["id"])


def test_spot_reserve_api_rejects_tampered_execution_and_reauthentication(tmp_path, monkeypatch):
    from app.automation import spot_reserve_comparison

    paper, state, lab, seed, strategy = harness(tmp_path)
    spot_paper_access.create(paper.path,SPOT_PAPER_PASSWORD)
    reserve = make_spot_reserve(lab,strategy)
    session = paper.create(strategy["version_id"],seed["id"])
    state["now"] = START+4*STEP+1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    client = TestClient(app)
    path = f"/paper/spot/strategies/{session['id']}/reserve-comparison?validation_id={reserve['id']}"
    assert client.get(path,headers={"Origin":"https://outside.invalid"}).status_code == 403
    response = client.get(path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store",response.text
    verify = spot_reserve_comparison.strategy_reference

    def rotate(*args):
        reference = verify(*args)
        spot_paper_access.lock(paper.path)
        spot_paper_access.unlock(paper.path,SPOT_PAPER_PASSWORD)
        return reference

    with monkeypatch.context() as changed:
        changed.setattr(spot_reserve_comparison,"strategy_reference",rotate)
        changed_session = client.get(path)
        assert changed_session.status_code == 422 and "cambió de sesión" in changed_session.text
    with paper.connect() as db:
        db.execute("UPDATE events SET payload_json=json_set(payload_json,'$.action.fee','0') WHERE session_id=?",
                   (session["id"],))
    tampered = client.get(path)
    assert tampered.status_code == 422 and "comisión" in tampered.text
    spot_paper_access.lock(paper.path)
    assert client.get(path).status_code == 423


def test_spot_research_export_keeps_decision_time_separate_from_later_results(tmp_path):
    from app.automation.spot_research_export import SCHEMA, spot_research_export
    from app.lab.store import content_hash

    paper, state, _, seed, strategy = harness(tmp_path)
    session = paper.create(strategy["version_id"], seed["id"])
    with pytest.raises(LabError, match="no hay observaciones"):
        spot_research_export(paper,session["id"])
    state["now"] = START+4*STEP+1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    early = spot_research_export(paper,session["id"])
    state["now"] += 31
    assert paper.poll(session["id"])["poll_result"] == "GAP"
    state["now"] += 1
    assert paper.close_position(session["id"])["close_result"] == "CLOSED"
    calls = state["quote_calls"],state["candle_calls"]
    preview = spot_research_export(paper,session["id"])
    assert set(preview) == {"schema","revision","record_count","event_count"}
    assert preview["schema"] == SCHEMA and preview["record_count"] == 4 and preview["event_count"] == 3
    with pytest.raises(LabError, match="cambió"):
        spot_research_export(paper,session["id"],early["revision"])
    result = spot_research_export(paper,session["id"],preview["revision"])
    assert result["sha256"] == hashlib.sha256(result["jsonl"].encode()).hexdigest()
    rows = [json.loads(line) for line in result["jsonl"].splitlines()]
    for row in rows:
        assert row["row_sha256"] == content_hash({key:value for key,value in row.items() if key!="row_sha256"})
    head,buy,gap,sell = rows
    assert head["environment"] == "PAPER" and head["execution_authority"] == "NONE"
    assert head["seed_sha256"] == seed["hash"] and len(head["seed_candles"]) == 3
    assert head["status_at_export"] == "GAP"
    assert buy["input"]["signal"] == {"entry":True,"exit":None}
    assert buy["input"]["quote"]["event_time_ns"] == buy["observed_at_ns"]
    assert set(buy["input"]) == {"candle","signal","position_open_before","quote"}
    assert buy["outcome"]["entry_decision"] == "EXECUTED_PAPER"
    assert buy["outcome"]["execution"]["pnl"] is None
    assert buy["outcome"]["execution"]["episode"] == sell["outcome"]["execution"]["episode"] == 1
    assert gap["kind"] == "RISK_COVERAGE_GAP" and gap["coverage"]["episode"] == 1
    assert gap["input"] is None and gap["outcome"] is None
    assert sell["kind"] == "MANUAL_CLOSE" and sell["outcome"]["execution"]["side"] == "SELL"
    assert sell["input"]["position_open_before"] is True and sell["outcome"]["execution"]["pnl"] is not None
    assert int(buy["observed_at_ns"]) < int(gap["observed_at_ns"]) < int(sell["observed_at_ns"])
    reopened = SpotStrategyPaper(paper.path, paper.lab.path, allow_create=False)
    assert spot_research_export(reopened,session["id"],preview["revision"]) == result
    assert (state["quote_calls"],state["candle_calls"]) == calls


def test_spot_research_export_includes_no_signal_without_raw_payload_and_enforces_size(tmp_path,monkeypatch):
    from app.automation import spot_research_export as export

    paper, state, lab, seed, _ = harness(tmp_path)
    strategy = lab.create_strategy("Dos velas alcistas",{
        **definition(),"entry":{"kind":"BULLISH_STREAK","period":2}})
    session = paper.create(strategy["version_id"],seed["id"])
    state["now"] = START+4*STEP+1
    observation = paper.poll(session["id"])["events"][0]
    assert not observation["entry_match"] and observation["action"] is None
    with paper.connect() as db:
        db.execute("""UPDATE events SET payload_json=json_set(payload_json,'$.note','private-marker',
            '$.quote.api_key','private-marker') WHERE session_id=?""",(session["id"],))
    preview = export.spot_research_export(paper,session["id"])
    result = export.spot_research_export(paper,session["id"],preview["revision"])
    assert "private-marker" not in result["jsonl"] and "api_key" not in result["jsonl"]
    event = json.loads(result["jsonl"].splitlines()[1])
    assert event["input"]["signal"] == {"entry":False,"exit":None}
    assert event["outcome"] == {"entry_decision":None,"execution":None}
    monkeypatch.setattr(export,"MAX_BYTES",32)
    with pytest.raises(LabError,match="supera"):
        export.spot_research_export(paper,session["id"],preview["revision"])
    with paper.connect() as db:
        db.execute("""UPDATE events SET payload_json=json_set(payload_json,'$.entry_status','EXECUTED_PAPER')
            WHERE session_id=?""",(session["id"],))
    with pytest.raises(LabError,match="Decisión"):
        export.spot_research_export(paper,session["id"])


def test_spot_research_export_api_requires_confirmation_same_revision_and_unlock(tmp_path,monkeypatch):
    from app.automation import spot_research_export as export

    paper, state, _, seed, strategy = harness(tmp_path)
    spot_paper_access.create(paper.path,SPOT_PAPER_PASSWORD)
    session = paper.create(strategy["version_id"],seed["id"])
    state["now"] = START+4*STEP+1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    app = FastAPI()
    app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
    client = TestClient(app)
    path = f"/paper/spot/strategies/{session['id']}/research-export"
    external = {"Origin":"https://outside.invalid"}
    assert client.get(path,headers=external).status_code == 403
    preview = client.get(path)
    assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
    body = {"revision":preview.json()["revision"],"confirmation":"EXPORTAR_JSONL_SPOT_PAPER"}
    assert client.post(path,json=body,headers=external).status_code == 403
    assert client.post(path,json={"revision":body["revision"]}).status_code == 422
    assert client.post(path,json={**body,"revision":"0"*64}).status_code == 422
    response = client.post(path,json=body)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["event_count"] == 1
    original = export.verified_spot_snapshot

    def rotate(*args):
        result = original(*args)
        spot_paper_access.lock(paper.path)
        spot_paper_access.unlock(paper.path,SPOT_PAPER_PASSWORD)
        return result

    with monkeypatch.context() as changed:
        changed.setattr(export,"verified_spot_snapshot",rotate)
        rotated = client.post(path,json=body)
        assert rotated.status_code == 422 and "jsonl" not in rotated.json()
    spot_paper_access.lock(paper.path)
    assert client.get(path).status_code == 423
    assert client.post(path,json=body).status_code == 423
