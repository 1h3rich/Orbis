"""DCA PAPER MEXC: saldo ficticio, reloj, límites y no retroactividad."""

from app.lab.schema import CURRENT_SCHEMA_VERSION
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import os
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.dca import AVERAGE_ENGINE_VERSION, DcaState, ENGINE_VERSION, apply_dca_step, simulate_dca, validate_dca
from app.automation.paper_dca import DAY, PaperDca, PaperDcaError, PaperDcaUnavailable, create_paper_dca_router
from app.automation.paper_dca_gap import analyze_dca_paper_gap
from app.automation.paper_worker import PaperDcaWorker
from app.automation.migration import (encrypt_active_automation, restore_secure_automation,
                                      verify_automation_database)
from app.automation.access import automation_access
from app.automation.store import AutomationStore
from app.lab.engine import LabError, validate_candles
from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.secure_backup import create_secure_lab_backup
from app.lab.store import LabStore, content_hash
from app.live_execution.dca_bot_source import DcaBotSource, SpotBotSourceError
from app.marketdata.adapters.mexc import MexcAdapter


START = 1_735_689_600


def test_empty_dca_reads_and_worker_do_not_create_plaintext_database(tmp_path):
    automation_path = tmp_path / "automation.db"
    store = AutomationStore(automation_path)
    paper = PaperDca(automation_path, tmp_path / "lab.db")

    assert store.rules() == []
    assert store.runs() == []
    assert paper.sessions() == []
    assert PaperDcaWorker(paper).run_once() == {}
    assert not automation_path.exists()
    assert not (tmp_path / "lab.db").exists()


def candles(prices=(100, 95, 80, 70, 65, 60)):
    return [{"t": START + i * DAY, "o": str(price + 1), "h": str(price + 2),
             "l": str(price - 1), "c": str(price), "v": str(10 + i)}
            for i, price in enumerate(prices)]


def rule(**changes):
    raw = {"frequency": "daily", "amount": "100", "budget_total": "1000",
           "monthly_limit": "300", "max_exposure": "1", "fee_rate": "0.001",
           "slippage_rate": "0.002", "max_extra_activations": 2,
           "cooldown_bars": 0, "dips": [{"drop_pct": "0.1", "amount": "50"},
                                           {"drop_pct": "0.2", "amount": "100"}]}
    return validate_dca(raw | changes)


def harness(tmp_path, *, prices=(100, 95, 80, 70, 65, 60)):
    state = {"now": START + 3 * DAY + 3600, "offline": False, "stale": False,
             "revised": False, "calls": 0, "quote_calls": 0}
    rows = candles(prices)

    class Source:
        def get_candles(self, symbol, timeframe="1d", limit=3, since=None, before=None):
            state["calls"] += 1
            state["candle_window"] = (since, before, limit)
            if state["offline"]:
                return None
            selected = [row.copy() for row in rows if since <= row["t"] < before][:limit]
            if state["revised"]:
                selected[0]["c"] = "94.5"
            return selected

        def get_paper_quote(self, symbol):
            state["quote_calls"] += 1
            event = state["now"] - (60 if state["stale"] else 1)
            return {"symbol": symbol, "source": "mexc-spot-public",
                    "event_time_ns": state.get("quote_event_ns", event * 1_000_000_000),
                    "bid": "70", "ask": "71", "last": "70"}

    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    lab, store = LabStore(lab_path), AutomationStore(automation_path)
    seed, gaps = validate_candles(rows[:3], "1d", now=state["now"])
    dataset = lab.save_dataset("BTC/USDT", "1d", "mexc-spot-public", seed, gaps, verified_public=True)
    version = store.create_rule("DCA BTC", rule())
    def clock_ns():
        ticks = state.get("clock_ticks")
        current = ticks.pop(0) if ticks else state["now"]
        return current * 1_000_000_000
    paper = PaperDca(automation_path, lab_path, adapter_factory=Source,
                     clock_ns=clock_ns)
    app = FastAPI()
    app.include_router(create_paper_dca_router(automation_path, lab_path, adapter_factory=Source,
                                              clock_ns=clock_ns))
    return paper, TestClient(app), state, store, lab, version, dataset, rows


def test_sma_paper_observes_next_close_and_archives_verified_window(tmp_path):
    paper, _, clock, store, lab, _, dataset, rows = harness(tmp_path, prices=(100, 95, 80, 110, 75, 85))
    gate = {"period": 4, "condition": "below"}
    version = store.create_rule("SMA PAPER", rule(frequency="monthly", sma_gate=gate))
    session = paper.create(version["version_id"], dataset["id"])
    assert session["sma_gate"] == gate and session["engine_version"] == "dca-1.7"
    sid = session["id"]
    clock["now"] = session["next_exec_t"] + 2
    rejected = paper.poll(sid)
    assert rejected["poll_result"] == "OBSERVED"
    assert clock["candle_window"] == (rows[0]["t"], session["next_exec_t"], 4)
    assert [entry["kind"] for entry in rejected["events"]] == ["OBSERVATION", "SKIP"]
    assert rejected["events"][0]["sma_window"] == rows[:4]
    assert rejected["events"][1]["reason"] == "SMA_CONDITION_NOT_MET"
    assert rejected["events"][1]["sma_value"] == "96.25"
    assert rejected["state"]["last_period"] is None
    clock["now"] += DAY
    accepted = paper.poll(sid)
    assert accepted["events"][2]["sma_window"] == rows[1:5]
    assert [(entry["trigger_kind"], entry["sma_value"]) for entry in accepted["events"]
            if entry["kind"] == "BUY"] == [("BASE", "90"), ("DIP", "90"), ("DIP", "90")]
    assert Decimal(accepted["state"]["spent"]) == 250
    reopened = PaperDca(store.path, lab.path, adapter_factory=paper.adapter_factory,
                        clock_ns=paper.clock_ns)
    assert reopened.detail(sid)["state"] == accepted["state"]
    with pytest.raises(SpotBotSourceError, match="solo está disponible"):
        DcaBotSource(reopened).observation(sid, now=datetime.fromtimestamp(clock["now"], timezone.utc))
    with store.connect() as db:
        observed = json.loads(db.execute(
            "SELECT event_json FROM paper_dca_events WHERE session_id=? AND sequence_no=0 ORDER BY execution_t DESC LIMIT 1",
            (sid,)).fetchone()[0])
        observed["sma_window"][0]["c"] = "94"
        db.execute("UPDATE paper_dca_events SET event_json=? WHERE session_id=? AND execution_t=? AND sequence_no=0",
                   (json.dumps(observed), sid, clock["now"] - 2))
    assert reopened.detail(sid)["state"] is None
    clock["now"] += DAY
    assert reopened.poll(sid)["poll_result"] == "GAP"


def test_volume_paper_archives_exact_windows_reopens_and_rejects_tampering(tmp_path):
    paper, _, clock, store, lab, _, dataset, rows = harness(tmp_path)
    rows[3]["v"], rows[4]["v"] = "5", "17"
    gate = {"period": 2, "min_ratio": "2"}
    version = store.create_rule("Volumen PAPER", rule(frequency="monthly", volume_gate=gate))
    session = paper.create(version["version_id"], dataset["id"])
    assert session["engine_version"] == "dca-1.9" and session["volume_gate"] == gate
    sid = session["id"]
    clock["now"] = session["next_exec_t"] + 2
    rejected = paper.poll(sid)
    assert rejected["events"][0]["volume_window"] == rows[1:4]
    assert {entry["reason"] for entry in rejected["events"][1:]} == {"VOLUME_BELOW_MINIMUM"}
    assert not rejected["state"]["last_period"]
    clock["now"] += DAY
    accepted = paper.poll(sid)
    assert accepted["state"] is not None
    assert [event["volume_window"] for event in accepted["events"] if event["kind"] == "OBSERVATION"] == [
        rows[1:4], rows[2:5]]
    assert [(event["trigger_kind"], event["volume_ratio"]) for event in accepted["events"] if event["kind"] == "BUY"] == [
        ("BASE", "2"), ("DIP", "2"), ("DIP", "2")]
    reopened = PaperDca(store.path, lab.path, adapter_factory=paper.adapter_factory, clock_ns=paper.clock_ns)
    assert reopened.detail(sid) == {key:value for key,value in accepted.items() if key != "poll_result"}
    assert sum(mark["count"] for mark in reopened.chart(sid)["buys"]) == 3
    with pytest.raises(SpotBotSourceError, match="no admite el filtro DCA"):
        DcaBotSource(reopened, allow_moving_average=True).observation(
            sid, now=datetime.fromtimestamp(clock["now"], timezone.utc))
    with store.connect() as db:
        event = db.execute("SELECT event_json FROM paper_dca_events WHERE session_id=? AND execution_t=? AND sequence_no=1",
                           (sid, clock["now"] - 2)).fetchone()
        payload = json.loads(event[0])
        payload["volume_ratio"] = "999"
        db.execute("UPDATE paper_dca_events SET event_json=? WHERE session_id=? AND execution_t=? AND sequence_no=1",
                   (json.dumps(payload), sid, clock["now"] - 2))
    assert reopened.detail(sid)["state"] is None
    clock["now"] += DAY
    assert reopened.poll(sid)["poll_result"] == "GAP"


def test_volume_paper_gap_replays_filter_without_posting_buy(tmp_path):
    paper, client, clock, store, _, _, dataset, rows = harness(tmp_path)
    rows[3]["v"], rows[4]["v"] = "5", "17"
    version = store.create_rule("Volumen hueco", rule(volume_gate={"period": 2, "min_ratio": "2"}))
    session = paper.create(version["version_id"], dataset["id"])
    sid = session["id"]
    clock["now"] = session["next_exec_t"] + DAY + 1
    assert paper.poll(sid)["poll_result"] == "GAP"
    before = paper.detail(sid)
    response = client.post(f"/automation/dca/paper/sessions/{sid}/gap-analyses")
    assert response.is_success, response.text
    report = response.json()
    assert report["steps"][0]["hypothetical_skips"][0]["reason"] == "VOLUME_BELOW_MINIMUM"
    assert report["steps"][1]["hypothetical_buys"][0]["volume_ratio"] == "2"
    assert report["paper_events_written"] == 0 and paper.detail(sid) == before


def test_sma_paper_gap_reuses_rolling_closes_and_never_changes_balance(tmp_path):
    paper, client, clock, store, _, _, dataset, rows = harness(tmp_path, prices=(100, 95, 80, 110, 75, 85))
    with pytest.raises(PaperDcaError, match="ventana SMA"):
        too_long = store.create_rule("SMA larga", rule(sma_gate={"period": 5, "condition": "below"}))
        paper.create(too_long["version_id"], dataset["id"])
    version = store.create_rule("SMA hueco", rule(frequency="monthly", sma_gate={"period": 4, "condition": "below"}))
    session = paper.create(version["version_id"], dataset["id"])
    clock["now"] = session["next_exec_t"] + 2 * DAY + 1
    assert paper.poll(session["id"])["poll_result"] == "GAP"
    before = paper.detail(session["id"])
    response = client.post(f"/automation/dca/paper/sessions/{session['id']}/gap-analyses")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["downloaded_candles"] == rows
    assert clock["candle_window"] == (rows[0]["t"], rows[-1]["t"] + DAY, len(rows))
    assert report["steps"][0]["hypothetical_skips"][0]["sma_value"] == "96.25"
    assert report["steps"][1]["hypothetical_buys"][0]["sma_value"] == "90"
    assert not report["steps"][2]["hypothetical_buys"]
    assert report["paper_events_written"] == 0
    assert paper.detail(session["id"]) == before


def test_sma_paper_unreadable_rule_hides_balance_and_stops_observation(tmp_path):
    paper, _, clock, store, _, _, dataset, _ = harness(tmp_path)
    version = store.create_rule("SMA inválida", rule(sma_gate={"period": 3, "condition": "below"}))
    session = paper.create(version["version_id"], dataset["id"])
    with store.connect() as db:
        db.execute("UPDATE paper_dca_sessions SET rule_json=? WHERE id=?",
                   (json.dumps(rule() | {"sma_gate": None}), session["id"]))
    assert paper.detail(session["id"])["state"] is None
    clock["now"] = session["next_exec_t"] + 1
    assert paper.poll(session["id"])["poll_result"] == "GAP"


@pytest.mark.parametrize("kind,factor,first_spend", [("ema", "1.5", "150"), ("sma", "0.5", "50")])
def test_paper_average_amounts_use_daily_close_and_keep_restart_state(tmp_path, kind, factor, first_spend):
    paper, client, clock, store, lab, _, dataset, _ = harness(tmp_path, prices=(100, 100, 160, 125, 90, 140))
    adjustment = {"kind": kind, "period": 3, "condition": "above",
                  "matched_factor": "1.5", "unmatched_factor": "0.5"}
    version = store.create_rule("DCA media móvil", rule(ma_adjustment=adjustment))
    session = paper.create(version["version_id"], dataset["id"])
    assert session["engine_version"] == AVERAGE_ENGINE_VERSION
    assert session["ma_adjustment"] == adjustment
    sid = session["id"]
    clock["now"] = session["next_exec_t"] + 1
    first = paper.poll(sid)
    observation, buy = first["events"]
    assert observation["average_window"][-1]["c"] == "125"
    assert buy["amount_factor"] == factor
    assert Decimal(buy["spent"]) == Decimal(first_spend)
    assert ("ema_origin" in observation) is (kind == "ema")
    if kind == "ema":
        assert Decimal(buy["ma_value"]) == Decimal("122.5")
    reopened = PaperDca(store.path, lab.path, adapter_factory=paper.adapter_factory, clock_ns=paper.clock_ns)
    assert reopened.detail(sid)["state"] == first["state"]
    clock["now"] += DAY
    second = reopened.poll(sid)
    assert [(entry["trigger_kind"], entry["amount_factor"], Decimal(entry["spent"]))
            for entry in second["events"] if entry["kind"] == "BUY"] == [
        ("BASE", factor, Decimal(first_spend)), ("BASE", "0.5", Decimal(50)), ("DIP", "0.5", Decimal(25))]
    if kind == "ema":
        assert Decimal(second["state"]["ema"]["current"]) == Decimal("106.25")
    assert sum(mark["count"] for mark in reopened.chart(sid)["buys"]) == 3
    clock["now"] = second["next_exec_t"] + 901
    before = reopened.poll(sid)
    assert before["poll_result"] == "GAP"
    saved = client.post(f"/automation/dca/paper/sessions/{sid}/gap-analyses")
    assert saved.status_code == 200, saved.text
    report = saved.json()
    hypothetical = report["steps"][0]["hypothetical_buys"][0]
    assert Decimal(hypothetical["requested"]) == 150
    assert Decimal(hypothetical["spent"]) == (75 if kind == "ema" else 150)
    recovered = reopened.saved_gap_analysis(sid, report["archive"]["id"])
    assert recovered["verified"] and recovered["result"]["steps"] == report["steps"]
    assert reopened.detail(sid)["state"] == before["state"]


def test_paper_ema_seed_tamper_fails_closed_before_first_tick(tmp_path):
    paper, _, clock, store, _, _, dataset, _ = harness(tmp_path)
    version = store.create_rule("EMA", rule(ma_adjustment={"kind": "ema", "period": 3,
        "condition": "below", "matched_factor": "2", "unmatched_factor": "0"}))
    session = paper.create(version["version_id"], dataset["id"])
    corrupt = session["state"]
    corrupt["ema"]["current"] = "1"
    with store.connect() as db:
        db.execute("UPDATE paper_dca_sessions SET state_json=? WHERE id=?",
                   (json.dumps(corrupt), session["id"]))
    with pytest.raises(PaperDcaError, match="semilla EMA"):
        paper.chart(session["id"])
    clock["now"] = session["next_exec_t"] + 2
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "GAP" and not observed["events"]
    assert clock["quote_calls"] == 0


def test_paper_chart_accepts_exact_calculated_amount_with_more_digits_than_inputs(tmp_path):
    paper, _, clock, store, _, _, dataset, _ = harness(tmp_path)
    value = "0.123456789012345678901234567890123456789"
    version = store.create_rule("Importe preciso", rule(amount=value, dips=[], ma_adjustment={
        "kind": "ema", "period": 3, "condition": "below", "matched_factor": value, "unmatched_factor": "0"}))
    session = paper.create(version["version_id"], dataset["id"])
    clock["now"] = session["next_exec_t"] + 1
    observed = paper.poll(session["id"])
    spent = observed["events"][1]["spent"]
    with localcontext() as context:
        context.prec = 300
        assert Decimal(spent) == Decimal(value) ** 2
    assert paper.chart(session["id"])["buys"][0]["spent"] == [spent]


def test_paper_dca_waits_for_quote_after_scheduled_execution(tmp_path):
    paper, _, state, _, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    execution_t = START + 4 * DAY
    state["now"] = execution_t + 1
    state["quote_event_ns"] = (execution_t - 5) * 1_000_000_000

    with pytest.raises(PaperDcaUnavailable, match="anterior a la ejecución"):
        paper.poll(session["id"])
    pending = paper.detail(session["id"])
    assert pending["status"] == "ACTIVE"
    assert pending["events"] == []

    state["quote_event_ns"] = execution_t * 1_000_000_000
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    assert sum(event["kind"] == "BUY" for event in observed["events"]) == 3


def test_paper_dca_comparison_requires_same_verified_run(tmp_path):
    paper, client, state, store, lab, version, dataset, rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    assert paper.comparison(session["id"])["historical"] is None
    bars, gaps = validate_candles(rows[:3], "1d", now=state["now"])
    assert not gaps
    result = simulate_dca(bars, version["definition"], timeframe="1d")
    run = store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                         result, ENGINE_VERSION, lab_path=lab.path,
                         expected_definition_hash=content_hash(version["definition"]))
    state["now"] = START + 4 * DAY + 1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    response = client.get(f"/automation/dca/paper/sessions/{session['id']}/comparison")
    assert response.status_code == 200, response.text
    compared = response.json()
    assert compared["historical"]["run_id"] == run["id"]
    assert compared["historical"]["verification"]["verified"] is True
    assert compared["historical"]["conditional_metrics"] == run["results"]["conditional"]["metrics"]
    assert compared["paper"]["observed_days"] == 1
    assert compared["paper"]["paper_buys"] == 3
    assert Decimal(compared["paper"]["spent_usdt"]) == 250
    assert compared["comparison_basis"] == "NOT_DIRECTLY_COMPARABLE"
    assert "delta" not in compared
    assert client.get(f"/automation/dca/paper/sessions/{session['id']}/comparison",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_paper_dca_pins_selected_verified_run_without_replacing_it(tmp_path):
    paper, client, state, store, lab, version, dataset, rows = harness(tmp_path)
    bars, _ = validate_candles(rows[:3], "1d", now=state["now"])
    result = simulate_dca(bars, version["definition"], timeframe="1d")
    original = store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                              result, ENGINE_VERSION, lab_path=lab.path,
                              expected_definition_hash=content_hash(version["definition"]))
    created = client.post("/automation/dca/paper/sessions", json={
        "version_id": version["version_id"], "dataset_id": dataset["id"],
        "source_run_id": original["id"]})
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["source_run_id"] == original["id"]
    newer = store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                           result, ENGINE_VERSION, lab_path=lab.path,
                           expected_definition_hash=content_hash(version["definition"]))
    comparison = paper.comparison(session["id"])
    assert comparison["source_run_id"] == original["id"]
    assert comparison["historical"]["run_id"] == original["id"] != newer["id"]
    assert comparison["historical"]["verification"]["verified"] is True
    other = store.add_version(version["id"], version["definition"])
    rejected = client.post("/automation/dca/paper/sessions", json={
        "version_id": other["version_id"], "dataset_id": dataset["id"],
        "source_run_id": original["id"]})
    assert rejected.status_code == 422
    with store.connect() as db:
        saved = json.loads(db.execute("SELECT result_json FROM dca_runs WHERE id=?",
                                      (newer["id"],)).fetchone()[0])
        saved["results"]["conditional"]["metrics"]["net_profit"] = "999999"
        db.execute("UPDATE dca_runs SET result_json=? WHERE id=?", (json.dumps(saved), newer["id"]))
    unverified = client.post("/automation/dca/paper/sessions", json={
        "version_id": version["version_id"], "dataset_id": dataset["id"],
        "source_run_id": newer["id"]})
    assert unverified.status_code == 422 and "no está verificada" in unverified.json()["detail"]
    with store.connect() as db:
        db.execute("DELETE FROM dca_runs WHERE id=?", (original["id"],))
    broken = paper.comparison(session["id"])["historical"]
    assert broken["run_id"] == original["id"]
    assert broken["conditional_metrics"] is None
    assert broken["verification"]["verified"] is False


def test_paper_dca_v4_migration_preserves_session_without_source_run(tmp_path):
    paper, _, _, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE paper_dca_sessions DROP COLUMN source_run_id")
        db.execute("DROP TABLE dca_validation_runs")
        db.execute("PRAGMA user_version=4")
    reopened = PaperDca(store.path, paper.lab.path)
    assert reopened.detail(session["id"])["source_run_id"] is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_paper_dca_rejects_balanced_spend_without_buy_event(tmp_path):
    paper, _client, state, _store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    with paper.store.connect() as db:
        saved = json.loads(db.execute("SELECT state_json FROM paper_dca_sessions WHERE id=?",
                                      (session["id"],)).fetchone()[0])
        saved.update({"cash": "900", "spent": "100", "monthly_spent": {"2025-01": "100"}})
        db.execute("UPDATE paper_dca_sessions SET state_json=? WHERE id=?",
                   (json.dumps(saved), session["id"]))
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert result["state"] is None
    assert result["events"] == []
    assert state["calls"] == state["quote_calls"] == 0


def test_paper_dca_rejects_missing_buy_event_before_next_observation(tmp_path):
    paper, _client, state, _store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 4 * DAY + 1
    first = paper.poll(session["id"])
    assert first["poll_result"] == "OBSERVED"
    with paper.store.connect() as db:
        db.execute("DELETE FROM paper_dca_events WHERE session_id=? AND sequence_no=1",
                   (session["id"],))
    state["now"] = START + 5 * DAY + 1
    before_calls = state["calls"], state["quote_calls"]
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert result["state"] is None
    assert (state["calls"], state["quote_calls"]) == before_calls
    with pytest.raises(PaperDcaError, match="Estado DCA"):
        paper.chart(session["id"])
    with pytest.raises(PaperDcaError, match="Estado DCA"):
        paper.comparison(session["id"])


def test_paper_dca_unreadable_event_remains_inspectable(tmp_path):
    paper, _client, state, _store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 4 * DAY + 1
    paper.poll(session["id"])
    with paper.store.connect() as db:
        db.execute("UPDATE paper_dca_events SET event_json='{' WHERE session_id=? AND sequence_no=1",
                   (session["id"],))
    result = paper.detail(session["id"])
    assert result["state"] is None
    assert result["events"][1]["unreadable"] is True


def test_paper_dca_rejects_event_changed_during_market_quote(tmp_path):
    paper, _client, state, _store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 4 * DAY + 1
    first = paper.poll(session["id"])
    assert first["poll_result"] == "OBSERVED"
    source = paper.adapter_factory

    class MutatingSource(source):
        def get_paper_quote(self, symbol):
            quote = super().get_paper_quote(symbol)
            with paper.store.connect() as db:
                db.execute("""UPDATE paper_dca_events SET event_json=json_set(event_json,'$.spent','999')
                              WHERE session_id=? AND sequence_no=1""", (session["id"],))
            return quote

    paper.adapter_factory = MutatingSource
    state["now"] = START + 5 * DAY + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert result["state"] is None
    assert len(result["events"]) == len(first["events"])


def test_gap_analysis_is_theoretical_and_never_books_missed_dca_buys(tmp_path):
    paper, client, state, _store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    url = f"/automation/dca/paper/sessions/{session['id']}/gap-analysis"
    assert client.get(url).status_code == 422
    assert client.get(url, headers={"Origin": "https://outside.invalid"}).status_code == 403
    state["now"] = START + 6 * DAY + 1
    assert paper.poll(session["id"])["status"] == "GAP"
    before = paper.detail(session["id"])
    response = client.get(url)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["scope"] == "THEORETICAL_DCA_POST_GAP_ONLY"
    assert report["paper_events_written"] == 0 and report["persisted"] is False
    assert report["missed_days"] == 3
    assert len(report["steps"]) == 3
    assert Decimal(report["theoretical_state"]["spent"]) > 0
    assert paper.detail(session["id"]) == before
    assert client.get(url).json() == report
    state["revised"] = True
    assert client.get(url).status_code == 422
    assert paper.detail(session["id"]) == before
    state["revised"] = False
    with paper.store.connect() as db:
        corrupt = json.loads(db.execute(
            "SELECT state_json FROM paper_dca_sessions WHERE id=?", (session["id"],)).fetchone()[0])
        corrupt["cash"] = "NaN"
        db.execute("UPDATE paper_dca_sessions SET state_json=? WHERE id=?",
                   (json.dumps(corrupt), session["id"]))
    invalid = client.get(url)
    assert invalid.status_code == 422 and "Estado DCA" in invalid.text


def test_gap_analysis_uses_pinned_precision_for_theoretical_branch():
    rows = [{"t": START + i * DAY, "o": "1", "h": "2", "l": "0.5",
             "c": "1", "v": "1"} for i in range(3)]
    initial = DcaState(cash=Decimal("9e40"), reference=Decimal("1")).json()
    snapshot = {"id": "gap-precision", "status": "GAP", "next_exec_t": START + 3 * DAY,
                "tick_index": 0, "overlap": rows[:2], "state": initial,
                "rule": rule(amount="1e-40", budget_total="9e40", monthly_limit="9e40",
                             fee_rate="0", slippage_rate="0", dips=[]),
                "dataset_hash": "fixture", "engine_version": ENGINE_VERSION}
    current = analyze_dca_paper_gap(snapshot, rows, now=START + 3 * DAY + 1)
    legacy = analyze_dca_paper_gap(snapshot | {"engine_version": "dca-1.4"}, rows,
                                   now=START + 3 * DAY + 1)
    assert current["paper_events_written"] == legacy["paper_events_written"] == 0
    with localcontext() as context:
        context.prec = 300
        assert Decimal(current["theoretical_state"]["cash"]) == Decimal("9e40") - Decimal("1e-40")
        assert Decimal(legacy["theoretical_state"]["cash"]) == Decimal("9e40")


def test_dca_gap_analysis_refuses_unbounded_historical_reconstruction(tmp_path):
    paper, client, state, _store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 95 * DAY + 1
    assert paper.poll(session["id"])["status"] == "GAP"
    calls = state["calls"]
    result = client.get(f"/automation/dca/paper/sessions/{session['id']}/gap-analysis")
    assert result.status_code == 422 and "cobertura" in result.text
    assert state["calls"] == calls


def test_dca_gap_archive_is_replayable_and_never_books_paper_buys(tmp_path):
    paper, client, state, store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 6 * DAY + 1
    assert paper.poll(session["id"])["status"] == "GAP"
    before = paper.detail(session["id"])
    path = f"/automation/dca/paper/sessions/{session['id']}/gap-analyses"
    first = client.post(path)
    assert first.status_code == 200, first.text
    saved = first.json()
    assert saved["scope"] == "THEORETICAL_DCA_POST_GAP_ONLY"
    assert saved["persisted"] is False and saved["archive"]["verified"] is True
    assert saved["paper_events_written"] == 0
    assert client.post(path).json()["archive"]["id"] == saved["archive"]["id"]
    assert paper.detail(session["id"]) == before
    report_id = saved["archive"]["id"]
    assert len(client.get(path).json()) == 1
    assert verify_automation_database(store.path)["counts"]["paper_dca_gap_reports"] == 1
    detail = client.get(f"{path}/{report_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["result"]["steps"] == saved["steps"]
    state["offline"] = True
    reopened = PaperDca(store.path, paper.lab.path, adapter_factory=lambda: None,
                        clock_ns=lambda: state["now"] * 1_000_000_000)
    assert reopened.saved_gap_analysis(session["id"], report_id)["hash"] == saved["archive"]["hash"]
    with store.connect() as db:
        material = json.loads(db.execute(
            "SELECT evidence_json FROM paper_dca_gap_reports WHERE id=?", (report_id,)).fetchone()[0])
        material["result"]["steps"][0]["close_proxy"] = "999"
        db.execute("UPDATE paper_dca_gap_reports SET evidence_json=? WHERE id=?",
                   (json.dumps(material), report_id))
    assert client.get(f"{path}/{report_id}").status_code == 422
    with pytest.raises(LabError, match="alterado"):
        verify_automation_database(store.path)
    assert paper.detail(session["id"]) == before


def test_dca_gap_archive_survives_encrypted_backup_and_restore(tmp_path):
    paper, _client, state, store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 6 * DAY + 1
    assert paper.poll(session["id"])["status"] == "GAP"
    archived = paper.save_gap_analysis(session["id"])["archive"]
    before = verify_automation_database(store.path)
    password = "test-only automation password"
    encrypted = encrypt_active_automation(store, "CIFRAR DCA", password)
    assert verify_automation_database(store.path, password) == before
    assert verify_automation_database(tmp_path / "automation-backups" / encrypted["backup"], password) == before
    restored = restore_secure_automation(store, encrypted["backup"], "RESTAURAR DCA", password)
    assert restored["verified"] == before
    try:
        automation_access.unlock(store.path, password)
        recovered = paper.saved_gap_analysis(session["id"], archived["id"])
        assert recovered["hash"] == archived["hash"] and recovered["verified"] is True
        assert paper.detail(session["id"])["events"] == []
    finally:
        automation_access.lock(store.path)


def test_dca_gap_keeps_multiple_day_scoped_reports_without_paper_events(tmp_path):
    paper, client, state, _store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 5 * DAY + 1
    assert paper.poll(session["id"])["status"] == "GAP"
    path = f"/automation/dca/paper/sessions/{session['id']}/gap-analyses"
    first = client.post(path).json()
    assert first["missed_days"] == 2
    state["now"] = START + 6 * DAY + 1
    second = client.post(path).json()
    assert second["missed_days"] == 3
    assert first["archive"]["id"] != second["archive"]["id"]
    listed = client.get(path).json()
    assert len(listed) == 2 and {item["missed_days"] for item in listed} == {2, 3}
    assert all("result" not in item and item["verified"] for item in listed)
    assert client.get(f"{path}/{first['archive']['id']}").json()["result"]["missed_days"] == 2
    assert client.get(f"{path}/{second['archive']['id']}").json()["result"]["missed_days"] == 3
    assert paper.detail(session["id"])["events"] == []


def test_dca_gap_archive_rejects_changed_original_session(tmp_path):
    paper, client, state, store, _lab, version, dataset, _rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 5 * DAY + 1
    assert paper.poll(session["id"])["status"] == "GAP"
    report_id = paper.save_gap_analysis(session["id"])["archive"]["id"]
    with store.connect() as db:
        db.execute("UPDATE paper_dca_sessions SET next_exec_t=next_exec_t+? WHERE id=?",
                   (DAY, session["id"]))
    response = client.get(f"/automation/dca/paper/sessions/{session['id']}/gap-analyses/{report_id}")
    assert response.status_code == 422 and "sesión original" in response.text
    assert client.get(f"/automation/dca/paper/sessions/{session['id']}/gap-analyses").status_code == 422


def test_paper_dca_single_active_session_is_atomic_and_can_restart_after_stop(tmp_path):
    paper, client, _state, _store, _lab, version, dataset, _rows = harness(tmp_path)
    args = (version["version_id"], dataset["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: _create_dca_once(paper, *args), range(2)))
    created = [item for item in attempts if isinstance(item, dict)]
    rejected = [item for item in attempts if isinstance(item, PaperDcaError)]
    assert len(created) == len(rejected) == 1
    assert "sesión PAPER activa" in str(rejected[0])
    assert len([item for item in paper.sessions() if item["status"] == "ACTIVE"]) == 1
    response = client.post("/automation/dca/paper/sessions", json={
        "version_id": args[0], "dataset_id": args[1]})
    assert response.status_code == 422
    assert paper.stop(created[0]["id"])["status"] == "STOPPED"
    restarted = paper.create(*args)
    assert restarted["id"] != created[0]["id"] and restarted["status"] == "ACTIVE"


def _create_dca_once(paper: PaperDca, version_id: str, dataset_id: str):
    try:
        return paper.create(version_id, dataset_id)
    except PaperDcaError as exc:
        return exc


def test_paper_dca_locked_encrypted_lab_retries_without_false_gap(tmp_path):
    paper, _, state, store, lab, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    password = "paper DCA encrypted LAB password"
    backup = create_secure_lab_backup(lab, password)
    os.replace(tmp_path / "lab-backups" / backup["name"], lab.path)
    state["now"] = START + 4 * DAY + 1
    lab_access.lock(lab.path)
    try:
        with pytest.raises(PaperDcaUnavailable, match="bloqueado"):
            paper.poll(session["id"])
        assert paper.detail(session["id"])["status"] == "ACTIVE"
        assert paper.detail(session["id"])["events"] == []
        assert state["calls"] == state["quote_calls"] == 0
        lab_access.unlock(lab.path, password)
        observed = paper.poll(session["id"])
        assert observed["poll_result"] == "OBSERVED"
        assert observed["status"] == "ACTIVE"
    finally:
        lab_access.lock(lab.path)


def test_paper_dca_comparison_hides_tampered_history(tmp_path):
    paper, _, state, store, lab, version, dataset, rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    bars, _ = validate_candles(rows[:3], "1d", now=state["now"])
    run = store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                         simulate_dca(bars, version["definition"], timeframe="1d"), ENGINE_VERSION,
                         lab_path=lab.path,
                         expected_definition_hash=content_hash(version["definition"]))
    with store.connect() as db:
        saved = json.loads(db.execute("SELECT result_json FROM dca_runs WHERE id=?", (run["id"],)).fetchone()[0])
        saved["results"]["conditional"]["metrics"]["net_profit"] = "999999"
        db.execute("UPDATE dca_runs SET result_json=? WHERE id=?", (json.dumps(saved), run["id"]))
    compared = paper.comparison(session["id"])
    assert compared["historical"]["verification"]["verified"] is False
    assert compared["historical"]["conditional_metrics"] is None


def test_paper_dca_shared_decision_persists_and_respects_budget(tmp_path):
    paper, client, state, store, lab, version, dataset, rows = harness(tmp_path)
    with lab.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert db.execute("SELECT count(*) FROM perp_datasets").fetchone()[0] == 0
    response = client.post("/automation/dca/paper/sessions",
                           json={"version_id": version["version_id"], "dataset_id": dataset["id"]})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    assert response.json()["next_exec_t"] == START + 4 * DAY
    assert client.post(f"/automation/dca/paper/sessions/{session_id}/poll").json()["poll_result"] == "WAIT"
    assert state["calls"] == 0
    state["now"] = START + 4 * DAY + 1
    observed = client.post(f"/automation/dca/paper/sessions/{session_id}/poll")
    assert observed.status_code == 200, observed.text
    result = observed.json()
    assert result["poll_result"] == "OBSERVED"
    assert result["execution"] == "NO_EXCHANGE_ORDERS"
    assert [event["kind"] for event in result["events"]] == ["OBSERVATION", "BUY", "BUY", "BUY"]
    assert [event["kind"] for event in result["events"][1:]] == ["BUY"] * 3
    assert [event["spent"] for event in result["events"][1:]] == ["100", "50", "100"]
    assert Decimal(result["state"]["cash"]) == 750
    assert Decimal(result["state"]["spent"]) == 250
    assert result["state"]["triggered"] == ["0.1", "0.2"]
    with localcontext() as context:
        context.prec = 80
        expected = DcaState(cash=Decimal("1000"), reference=Decimal("100"))
        event, skipped = apply_dca_step(expected, rule(), signal_time=rows[3]["t"],
            signal_close=Decimal("70"), execution_time=START + 4 * DAY,
            execution_price=Decimal("71"), mark_price=Decimal("70"), index=1)
    assert expected.json() == result["state"] and skipped == []
    assert [row["spent"] for row in event] == ["100", "50", "100"]
    assert paper.detail(session_id)["events"] == result["events"]
    assert PaperDca(store.path, lab.path).detail(session_id)["state"] == result["state"]
    assert client.post(f"/automation/dca/paper/sessions/{session_id}/poll").json()["poll_result"] == "WAIT"
    assert state["calls"] == 1
    state["now"] = START + 5 * DAY + 1
    second = paper.poll(session_id)
    assert second["poll_result"] == "OBSERVED"
    assert [row["spent"] for row in second["events"] if row["kind"] == "BUY"] == ["100", "50", "100", "50"]
    assert Decimal(second["state"]["spent"]) == 300
    assert Decimal(second["state"]["cash"]) == 700


def test_paper_dca_chart_uses_only_pinned_seed_and_observed_buys(tmp_path):
    paper, client, state, store, _lab, version, dataset, rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    path = f"/automation/dca/paper/sessions/{session['id']}/chart"
    initial = client.get(path)
    assert initial.status_code == 200, initial.text
    assert initial.json()["candles"] == rows[:3]
    assert initial.json()["buys"] == []
    assert initial.json()["environment"] == "PAPER"
    assert client.get(path, headers={"Origin": "https://outside.invalid"}).status_code == 403
    state["now"] = START + 4 * DAY + 1
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    calls = state["calls"]
    chart = client.get(path)
    assert chart.status_code == 200, chart.text
    data = chart.json()
    assert data["seed_count"] == 3 and data["observed_count"] == 1
    assert data["candles"] == rows[:4]
    assert data["buys"] == [{"execution_t": START + 4 * DAY,
                             "signal_t": START + 3 * DAY,
                             "price": data["buys"][0]["price"], "count": 3,
                             "triggers": ["BASE", "DIP", "DIP"],
                             "spent": ["100", "50", "100"]}]
    assert data["buys"][0]["execution_t"] > data["candles"][-1]["t"]
    assert state["calls"] == calls  # No consulta MEXC ni rellena velas.
    with store.connect() as db:
        row = db.execute("SELECT event_json FROM paper_dca_events WHERE session_id=? AND sequence_no=0",
                         (session["id"],)).fetchone()
        altered = json.loads(row[0])
        altered["signal_candle"]["t"] -= DAY
        db.execute("UPDATE paper_dca_events SET event_json=? WHERE session_id=? AND sequence_no=0",
                   (json.dumps(altered), session["id"]))
    assert client.get(path).status_code == 422


@pytest.mark.parametrize("rolled_back_to", (START + 4 * DAY - 1, START + 4 * DAY))
def test_clock_rollback_after_quote_cannot_book_early_paper_buy(tmp_path, rolled_back_to):
    paper, _, state, _, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    due = START + 4 * DAY
    state["now"] = due + 1
    state["clock_ticks"] = [due + 1, due + 1, due + 1, rolled_back_to]
    with pytest.raises(PaperDcaUnavailable, match="Reloj local retrocedió"):
        paper.poll(session["id"])
    unchanged = paper.detail(session["id"])
    assert unchanged["status"] == "ACTIVE"
    assert unchanged["events"] == []
    assert unchanged["state"]["spent"] == "0"
    state["now"] = due + 2
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"


def test_paper_dca_exposure_cap_and_cooldown_share_historical_math(tmp_path):
    paper, _, state, store, _, _, dataset, _ = harness(tmp_path)
    limited = store.create_rule("Riesgo acotado", rule(max_exposure="0.1", cooldown_bars=2))
    session = paper.create(limited["version_id"], dataset["id"])
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    buys = [row for row in result["events"] if row["kind"] == "BUY"]
    assert buys and Decimal(result["state"]["spent"]) < 150
    assert any(row["kind"] == "SKIP" and row["reason"] == "ACTIVATION_OR_COOLDOWN_LIMIT"
               for row in result["events"])
    mark = Decimal(result["last_quote"]["bid"])
    state_row = result["state"]
    equity = Decimal(state_row["cash"]) + Decimal(state_row["units"]) * mark
    assert Decimal(state_row["units"]) * mark <= equity * Decimal("0.1")


def test_paper_high_watermark_is_seeded_from_closed_history(tmp_path):
    paper, _, state, store, _, _, dataset, rows = harness(
        tmp_path, prices=(100, 150, 120, 120, 110, 100))
    version = store.create_rule("Máximo observado", rule(reference_mode="high_watermark"))
    session = paper.create(version["version_id"], dataset["id"])
    assert session["reference_mode"] == "high_watermark"
    assert session["engine_version"] == "dca-1.6"
    assert session["state"]["reference"] == "150"
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["poll_result"] == "OBSERVED"
    dips = [event for event in result["events"] if event["kind"] == "BUY"
            and event["trigger_kind"] == "DIP"]
    assert [event["drop_pct"] for event in dips] == ["0.1", "0.2"]
    assert all(event["signal_time"] == rows[3]["t"] for event in dips)
    assert result["state"]["reference"] == "150"


def test_existing_dca_11_paper_session_keeps_original_reference(tmp_path):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    with store.connect() as db:
        db.execute("UPDATE paper_dca_sessions SET engine_version='dca-1.1' WHERE id=?",
                   (session["id"],))
    state["now"] = START + 4 * DAY + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    assert observed["events"][0]["engine_version"] == "dca-1.1"
    assert observed["state"]["reference"] == "100"


@pytest.mark.parametrize("target", ("session", "version"))
def test_paper_dca_stops_before_network_if_saved_rule_changes(tmp_path, target):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    altered = rule(amount="200")
    with store.connect() as db:
        if target == "session":
            db.execute("UPDATE paper_dca_sessions SET rule_json=? WHERE id=?",
                       (json.dumps(altered), session["id"]))
        else:
            db.execute("UPDATE dca_versions SET definition_json=? WHERE id=?",
                       (json.dumps(altered), version["version_id"]))
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["status"] == "GAP"
    assert "regla DCA cambió" in result["status_reason"]
    assert result["events"] == []
    assert state["calls"] == state["quote_calls"] == 0


def test_paper_dca_stops_before_network_if_dataset_changes(tmp_path):
    paper, _, state, store, lab, version, dataset, rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    changed = [row.copy() for row in rows[:3]]
    changed[1]["c"] = "96"
    with lab.connect() as db:
        db.execute("UPDATE datasets SET candles_json=? WHERE id=?",
                   (json.dumps(changed), dataset["id"]))
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["status"] == "GAP"
    assert "no verificables" in result["status_reason"]
    assert result["events"] == []
    assert state["calls"] == state["quote_calls"] == 0


@pytest.mark.parametrize("field", ("rule_json", "overlap_json"))
def test_paper_dca_unreadable_inputs_stop_cleanly(tmp_path, field):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    with store.connect() as db:
        db.execute(f"UPDATE paper_dca_sessions SET {field}=? WHERE id=?",
                   ("{malformed", session["id"]))
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["poll_result"] == "GAP"
    assert "ilegible" in result["status_reason"]
    assert result["events"] == []
    assert state["calls"] == state["quote_calls"] == 0


@pytest.mark.parametrize("change", (
    {"cash": "NaN"},
    {"cash": "-1"},
    {"cash": "1200"},
    {"monthly_spent": {"2025-01": "20"}},
))
def test_paper_dca_corrupt_state_stops_before_market_fetch(tmp_path, change):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    with store.connect() as db:
        saved = json.loads(db.execute(
            "SELECT state_json FROM paper_dca_sessions WHERE id=?", (session["id"],)).fetchone()[0])
        db.execute("UPDATE paper_dca_sessions SET state_json=? WHERE id=?",
                   (json.dumps(saved | change), session["id"]))
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert "Estado DCA" in result["status_reason"]
    assert result["events"] == []
    assert state["calls"] == state["quote_calls"] == 0


def test_paper_dca_unreadable_state_stops_and_remains_inspectable(tmp_path):
    paper, client, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    with store.connect() as db:
        db.execute("UPDATE paper_dca_sessions SET state_json=? WHERE id=?",
                   ("{malformed", session["id"]))
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert result["state"] is None and result["events"] == []
    assert state["calls"] == state["quote_calls"] == 0
    response = client.get(f"/automation/dca/paper/sessions/{session['id']}")
    assert response.status_code == 200 and response.json()["state"] is None


def test_paper_dca_state_change_during_market_fetch_stops_without_buy(tmp_path):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    source = paper.adapter_factory

    class MutatingSource(source):
        def get_paper_quote(self, symbol):
            quote = super().get_paper_quote(symbol)
            with store.connect() as db:
                saved = json.loads(db.execute(
                    "SELECT state_json FROM paper_dca_sessions WHERE id=?", (session["id"],)).fetchone()[0])
                db.execute("UPDATE paper_dca_sessions SET state_json=? WHERE id=?",
                           (json.dumps(saved | {"cash": "900", "spent": "100",
                                                "monthly_spent": {"2025-01": "100"}}), session["id"]))
            return quote

    paper.adapter_factory = MutatingSource
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["status"] == result["poll_result"] == "GAP"
    assert "durante la observación" in result["status_reason"]
    assert result["events"] == []
    assert state["calls"] == state["quote_calls"] == 1


@pytest.mark.parametrize("target", ("session", "version", "dataset"))
def test_paper_dca_rechecks_inputs_after_market_fetch(tmp_path, target):
    paper, _, state, store, lab, version, dataset, rows = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    source = paper.adapter_factory

    class MutatingSource(source):
        def get_paper_quote(self, symbol):
            quote = super().get_paper_quote(symbol)
            if target == "dataset":
                changed = [row.copy() for row in rows[:3]]
                changed[1]["c"] = "96"
                with lab.connect() as db:
                    db.execute("UPDATE datasets SET candles_json=? WHERE id=?",
                               (json.dumps(changed), dataset["id"]))
            else:
                with store.connect() as db:
                    if target == "session":
                        db.execute("UPDATE paper_dca_sessions SET rule_json=? WHERE id=?",
                                   (json.dumps(rule(amount="200")), session["id"]))
                    else:
                        db.execute("UPDATE dca_versions SET definition_json=? WHERE id=?",
                                   (json.dumps(rule(amount="200")), version["version_id"]))
            return quote

    paper.adapter_factory = MutatingSource
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["poll_result"] == "GAP"
    assert result["events"] == []
    assert "durante la observación" in result["status_reason"]
    assert state["calls"] == state["quote_calls"] == 1


def test_late_gap_outage_stale_quote_and_manual_stop(tmp_path):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    second_version = store.add_version(version["id"], version["definition"])
    other = paper.create(second_version["version_id"], dataset["id"])
    state["now"] = START + 4 * DAY + 1
    state["offline"] = True
    with pytest.raises(PaperDcaUnavailable, match="tres velas"):
        paper.poll(session["id"])
    assert paper.detail(session["id"])["status"] == "ACTIVE"
    state["offline"] = False
    state["stale"] = True
    with pytest.raises(PaperDcaUnavailable, match="obsoleto"):
        paper.poll(session["id"])
    assert paper.detail(session["id"])["events"] == []
    state["stale"] = False
    assert paper.stop(session["id"])["status"] == "STOPPED"
    assert paper.poll(session["id"])["poll_result"] == "STOPPED"
    state["now"] = START + 4 * DAY + 901
    assert paper.poll(other["id"])["poll_result"] == "GAP"
    assert paper.detail(other["id"])["events"] == []


def test_revised_overlap_and_concurrent_poll_do_not_duplicate(tmp_path):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    second_version = store.add_version(version["id"], version["definition"])
    other = paper.create(second_version["version_id"], dataset["id"])
    state["now"] = START + 4 * DAY + 1
    state["revised"] = True
    assert paper.poll(session["id"])["status"] == "GAP"
    assert paper.detail(session["id"])["events"] == []
    state["revised"] = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: paper.poll(other["id"]), range(2)))
    assert len(paper.detail(other["id"])["events"]) == 4
    assert {row["poll_result"] for row in results} <= {"OBSERVED", "CHANGED", "WAIT"}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM paper_dca_events WHERE session_id=?", (other["id"],)).fetchone()[0] == 4


def test_import_spoof_and_wrong_market_blocked_and_v1_migration_preserves_rules(tmp_path):
    paper, client, state, store, lab, version, dataset, rows = harness(tmp_path)
    bars, _ = validate_candles(rows[:3], "1d", now=state["now"])
    spoof = lab.save_dataset("BTC/USDT", "1d", "mexc-spot-public", bars, [])
    assert spoof["verified_public"] is False
    with pytest.raises(PaperDcaError, match="descargado"):
        paper.create(version["version_id"], spoof["id"])
    assert client.get("/automation/dca/paper/sessions",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE paper_dca_gap_reports")
        db.execute("DROP TABLE paper_dca_events")
        db.execute("DROP TABLE paper_dca_sessions")
        db.execute("DROP TABLE dca_validation_runs")
        db.execute("PRAGMA user_version=1")
    assert store.version(version["version_id"])["definition"] == rule()
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_engine_mismatch_stops_session_without_reinterpreting_rule(tmp_path):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE paper_dca_sessions SET engine_version='dca-older' WHERE id=?", (session["id"],))
    state["now"] = START + 4 * DAY + 1
    result = paper.poll(session["id"])
    assert result["status"] == "GAP" and result["events"] == []
    assert "motor" in result["status_reason"]


def test_local_worker_retries_then_observes_once_and_marks_missed_day(tmp_path):
    paper, _, state, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    worker = PaperDcaWorker(paper)
    assert worker.run_once() == {}
    state["now"] = START + 4 * DAY + 1
    state["offline"] = True
    assert worker.run_once()[session["id"]] == "RETRY"
    assert paper.detail(session["id"])["events"] == []
    state["offline"] = False
    assert worker.run_once()[session["id"]] == "OBSERVED"
    assert len(paper.detail(session["id"])["events"]) == 4
    assert worker.run_once() == {}
    restarted = PaperDcaWorker(PaperDca(store.path, paper.lab.path,
                                        adapter_factory=paper.adapter_factory,
                                        clock_ns=paper.clock_ns))
    assert restarted.run_once() == {}
    state["now"] = START + 5 * DAY + 901
    assert restarted.run_once()[session["id"]] == "GAP"
    assert len(paper.detail(session["id"])["events"]) == 4


def test_local_worker_background_wake_and_shutdown(tmp_path):
    import time

    paper, _, state, _, _, version, dataset, _ = harness(tmp_path)
    worker = PaperDcaWorker(paper, retry_seconds=0.01, idle_seconds=0.1)
    worker.start()
    try:
        session = paper.create(version["version_id"], dataset["id"])
        state["now"] = START + 4 * DAY + 1
        worker.wake()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not paper.detail(session["id"])["events"]:
            time.sleep(0.01)
        assert len(paper.detail(session["id"])["events"]) == 4
        assert worker.snapshot()["running"] is True
    finally:
        worker.stop()
    assert worker.snapshot()["running"] is False


def test_v2_paper_migration_marks_unpinned_active_session_as_gap(tmp_path):
    paper, _, _, store, _, version, dataset, _ = harness(tmp_path)
    session = paper.create(version["version_id"], dataset["id"])
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE paper_dca_gap_reports")
        db.execute("ALTER TABLE paper_dca_sessions DROP COLUMN engine_version")
        db.execute("ALTER TABLE paper_dca_sessions DROP COLUMN source_run_id")
        db.execute("DROP TABLE dca_validation_runs")
        db.execute("PRAGMA user_version=2")
    recovered = PaperDca(store.path, paper.lab.path).detail(session["id"])
    assert recovered["status"] == "GAP"
    assert recovered["engine_version"] == "UNPINNED"
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_mexc_quote_contract_rejects_other_symbol_and_exposes_event_time():
    sample = {"symbol": "BTCUSDT", "lastPrice": "70", "bidPrice": "69", "askPrice": "71",
              "closeTime": 1_735_689_600_000}
    adapter = MexcAdapter(http_get=lambda _: sample)
    assert adapter.get_paper_quote("BTC/USDT")["event_time_ns"] == 1_735_689_600_000_000_000
    sample["symbol"] = "ETHUSDT"
    assert adapter.get_paper_quote("BTC/USDT") is None


def test_lab_download_marks_mexc_public_history_verified(tmp_path, monkeypatch):
    import app.lab.api as lab_api

    rows = candles()[:3]

    class Source:
        known_pairs = ("BTC/USDT",)

        def get_candles(self, symbol, timeframe="1d", limit=200, since=None, before=None):
            return [row for row in rows if row["t"] >= since][:limit]

    monkeypatch.setattr(lab_api, "MexcAdapter", Source)
    app = FastAPI()
    path = tmp_path / "lab.db"
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "paper dca lab download password"}).status_code == 200
    response = client.post("/lab/datasets/download", json={"symbol": "BTC/USDT",
        "timeframe": "1d", "provider": "mexc", "start": START, "end": START + 3 * DAY})
    assert response.status_code == 200, response.text
    meta = response.json()
    assert meta["source"] == "mexc-spot-public" and meta["verified_public"] is True
    assert LabStore(path).dataset(meta["id"])[1] == rows
