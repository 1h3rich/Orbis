"""LAB→PAPER: observación causal y ejecución ficticia optativa."""

from app.derivatives.paper_schema import CURRENT_PAPER_SCHEMA
from tests.paper_schema_fixture import drop_observed_tables
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import sqlite3
import time

import pytest
import sqlcipher3
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.funding import HOUR_NS
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.paper_access import paper_perp_access, paper_perp_format
from app.derivatives.paper_api import create_paper_perp_router
from app.derivatives.paper_migration import (
    encrypt_active_paper_perp, restore_secure_paper_perp, verify_paper_perp_database,
)
from app.derivatives.paper_math import MarginRules, PaperPerpError, PerpQuote
from app.derivatives.signal_monitor import (SignalMonitor, SignalMonitorError,
                                            SignalMonitorUnavailable, create_signal_monitor_router)
from app.derivatives.signal_worker import SignalWorker
from app.derivatives.signal_risk_worker import SignalRiskWorker
from app.lab.engine import Candle, IndicatorCache, LabError, evaluate, run_backtest, validate_candles, validate_strategy
from app.lab.store import LabStore
from app.lab.paper_indicator_state import paper_indicator_state_matches
from app.marketdata.adapters.grvt import GrvtHistoryError
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits
from app.live_execution.grvt_testnet_signer import GrvtTestnetSignError
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
from app.live_execution.grvt_testnet_signal import (
    GrvtTestnetSignalError, client_id_for_signal, submit_signal_testnet_once,
    sign_and_submit_signal_testnet_once,
)


STEP = 3600
START = 1_735_689_600


def candles():
    closes = [100, 99, 98, 97, 96, 95, 94]
    return [{"t": START + i * STEP, "o": str(close + 1), "h": str(close + 2),
             "l": str(close - 1), "c": str(close), "v": str(10 + i)}
            for i, close in enumerate(closes)]


def definition(direction="SHORT"):
    return {"direction": direction, "initial_capital": "1000", "allocation": "0.1",
            "fee_rate": "0.001", "slippage_rate": "0", "stop_loss": "0.03",
            "take_profit": "0.08", "entry": {"kind": "BEARISH_STREAK", "period": 3},
            "exit": {"kind": "VOLUME_RISING_STREAK", "period": 3}}


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_grvt_paper_time_limit_closes_after_observed_candle(tmp_path, direction):
    monitor, _, state, paper, lab, account, dataset, _, _rows = harness(tmp_path)
    rules = {key: value for key, value in definition(direction).items() if key != "exit"}
    limited = lab.create_strategy("Permanencia LAB", validate_strategy({
        **rules, "stop_loss": "0.9", "take_profit": "0.9", "max_holding_bars": 1}))
    session = monitor.create(account["id"], limited["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    first = monitor.poll(session["id"])
    assert first["poll_result"] == "OBSERVED"
    assert first["actions"][0]["action"] == "OPEN"
    assert first["actions"][0]["status"] == "EXECUTED"
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 7 * STEP + 10)
    second = monitor.poll(session["id"])
    assert second["poll_result"] == "OBSERVED"
    assert second["actions"][1]["action"] == "CLOSE"
    assert second["actions"][1]["reason"] == "TIME_LIMIT_AT_CANDLE_CLOSE"
    assert second["actions"][1]["status"] == "EXECUTED"
    assert paper.position(account["id"], first["actions"][0]["position_id"])["closed_at_ns"] is not None
    assert len(SignalMonitor(paper.path, lab.path).detail(session["id"])["actions"]) == 2


def test_grvt_paper_time_limit_never_reconstructs_missing_risk_feed(tmp_path):
    monitor, _, state, _, lab, account, dataset, _, _rows = harness(tmp_path)
    rules = {key: value for key, value in definition().items() if key != "exit"}
    limited = lab.create_strategy("Permanencia LAB", {**rules, "max_holding_bars": 1})
    session = monitor.create(account["id"], limited["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    monitor.poll(session["id"])
    state["now"] = START + 7 * STEP + 10
    result = monitor.poll(session["id"])
    assert result["poll_result"] == "GAP"
    assert len(result["actions"]) == 1


def test_grvt_signal_detail_exposes_remaining_candle_capacity(tmp_path):
    monitor, _, state, _, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    assert monitor.detail(session["id"])["capacity_remaining"] == 4995
    state["now"] = START + 6 * STEP + 1
    assert monitor.poll(session["id"])["capacity_remaining"] == 4994


def test_grvt_paper_time_limit_missing_entry_clock_fails_closed(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _rows = harness(tmp_path)
    rules = {key: value for key, value in definition().items() if key != "exit"}
    limited = lab.create_strategy("Permanencia LAB", {**rules, "max_holding_bars": 1})
    session = monitor.create(account["id"], limited["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    monitor.poll(session["id"])
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_actions SET status='EXPIRED' WHERE session_id=? AND action='OPEN'",
                   (session["id"],))
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 7 * STEP + 10)
    result = monitor.poll(session["id"])
    assert result["poll_result"] == "GAP"
    assert result["status_reason"] == "Inicio de posición PAPER sin vela verificable; cierre manual necesario"
    assert len(result["actions"]) == 1


def test_ema_cross_signal_matches_lab_on_same_closed_candle(tmp_path):
    monitor, _, state, _, lab, account, dataset, _, all_rows = harness(tmp_path)
    ema_rule = {"kind": "EMA_CROSS_ABOVE_EMA", "period": 2, "slow_period": 3}
    strategy = lab.create_strategy("Cruce EMA PAPER", {
        **definition("LONG"), "entry": ema_rule,
    })
    # La quinta vela observada cambia el orden de las medias. La fuente
    # ficticia conserva idénticas las dos velas de solape ya archivadas.
    all_rows[5] = {**all_rows[5], "o": "100", "h": "111", "l": "94", "c": "110"}
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 10
    observed = monitor.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    event = observed["observations"][-1]
    bars, gaps = validate_candles(all_rows[:6], "1h", now=state["now"])
    assert gaps == []
    expected = evaluate(validate_strategy(strategy["definition"])["entry"],
                        bars, 5, IndicatorCache(bars))
    assert event["entry_match"] == expected[0] is True
    assert event["evidence"]["entry"] == expected[1]
    assert observed["actions"] == []
    assert observed["execution"] == "SIGNALS_ONLY_NO_ORDERS"


def test_grvt_auto_paper_waits_for_ticker_after_signal_candle_close(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    due = START + 6 * STEP
    state["now"] = due + 1
    state["ticker_event_ns"] = (due - 5) * 1_000_000_000

    with pytest.raises(SignalMonitorError, match="anterior al cierre"):
        monitor.poll(session["id"])
    pending = monitor.detail(session["id"])
    assert pending["status"] == "ACTIVE"
    assert len(pending["observations"]) == 1
    assert pending["actions"][0]["status"] == "PENDING"
    assert paper.account(account["id"])["open_positions"] == 0

    state["ticker_event_ns"] = (due + 1) * 1_000_000_000
    retried = monitor.poll(session["id"])
    assert retried["actions"][0]["status"] == "EXECUTED"
    assert paper.account(account["id"])["open_positions"] == 1


def test_grvt_observer_agrees_with_lab_on_ascending_volume_window(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, all_rows = harness(
        tmp_path, volumes=(10, 11, 12, 50, 10, 20, 30))
    rule = {"kind": "VOLUME_ASCENDING_BARS", "period": 3}
    strategy = lab.create_strategy("Volumen GRVT", validate_strategy({
        **definition("SHORT"), "entry": rule}))
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 10
    first = monitor.poll(session["id"])
    assert first["poll_result"] == "OBSERVED"
    assert first["observations"][-1]["entry_match"] is False
    state["now"] = START + 7 * STEP + 10
    second = monitor.poll(session["id"])
    assert second["poll_result"] == "OBSERVED"
    assert second["observations"][-1]["entry_match"] is True
    bars, _ = validate_candles(all_rows, "1h", now=state["now"])
    assert evaluate(rule, bars, 6)[0] is True
    assert evaluate({"kind": "VOLUME_RISING_STREAK", "period": 3}, bars, 6)[0] is False
    assert monitor.comparison(session["id"])["signal_replay"]["verified"] is True
    assert paper.account(account["id"])["open_positions"] == 0


def harness(tmp_path, *, volumes=None):
    paper_path, lab_path = tmp_path / "paper.db", tmp_path / "lab.db"
    state = {"now": START + 5 * STEP + 10, "offline": False, "revised": False, "calls": 0,
             "ticker_offline": False, "rules_offline": False, "rules_calls": 0,
             "rules_maintenance": "0.01",
             "rules_initial": "0.02", "bid": "99", "ask": "101", "mark": "100"}
    all_rows = candles()
    if volumes is not None:
        for row, volume in zip(all_rows, volumes, strict=True):
            row["v"] = str(volume)

    class Source:
        known_pairs = ("BTC/USDT:PERP",)

        def _instrument(self, symbol):
            return "BTC_USDT_Perp" if symbol == "BTC/USDT:PERP" else None

        def get_ticker(self, symbol):
            if state["ticker_offline"]:
                return None
            return {"instrument": "BTC_USDT_Perp", "event_time_ns": state.get("ticker_event_ns", state["now"] * 1_000_000_000),
                    "mark_price": state["mark"], "bid": state["bid"], "ask": state["ask"]}

        def get_margin_rules(self, symbol):
            state["rules_calls"] += 1
            if state["rules_offline"]:
                return None
            return {"instrument": "BTC_USDT_Perp", "fetched_at_ns": state["now"] * 1_000_000_000,
                    "max_position_size": "1000", "risk_brackets": [
                        {"tier": 1, "notional_floor": "0", "notional_cap": "600000",
                         "maintenance_margin_rate": state["rules_maintenance"],
                         "initial_margin_rate": state["rules_initial"],
                         "max_leverage": 50, "cumulative_maintenance_amount": "0"}]}

        def get_instrument_spec(self, symbol):
            return {"instrument": "BTC_USDT_Perp", "kind": "PERPETUAL", "venues": ["ORDERBOOK"],
                    "base_decimals": 3, "tick_size": "0.01", "min_size": "0.01",
                    "min_notional": "20", "max_position_size": "1000",
                    "fetched_at_ns": state["now"] * 1_000_000_000}

        def get_historical_candles(self, symbol, timeframe, start, end, *, now):
            state["calls"] += 1
            if state["offline"]:
                raise GrvtHistoryError("red caída")
            rows = [row.copy() for row in all_rows if start <= row["t"] < end]
            if state["revised"]:
                rows[0]["c"] = "97.5"
            return rows

    paper = PaperPerpStore(paper_path, clock_ns=lambda: state["now"] * 1_000_000_000)
    account = paper.create_account("Prueba", "1000", "500", "800", "5", 2, "0.001", "0")
    lab = LabStore(lab_path)
    bars, _ = validate_candles(all_rows[:5], "1h", now=state["now"])
    dataset = lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public", bars, [], verified_public=True)
    strategy = lab.create_strategy("Short causal", definition())
    monitor = SignalMonitor(paper_path, lab_path, adapter_factory=Source,
                            clock_ns=lambda: state["now"] * 1_000_000_000)
    app = FastAPI()
    app.include_router(create_signal_monitor_router(paper_path, lab_path, adapter_factory=Source,
                                                    clock_ns=lambda: state["now"] * 1_000_000_000))
    return monitor, TestClient(app), state, paper, lab, account, dataset, strategy, all_rows


@pytest.mark.parametrize(("direction", "bar", "mark", "expected_level"), [
    ("LONG", ("95", "120", "94", "110"), "108", "114.00"),
    ("SHORT", ("95", "100", "80", "95"), "90", "84.00"),
])
def test_grvt_paper_trailing_stop_replays_after_restart_and_uses_mark(
        tmp_path, direction, bar, mark, expected_level):
    monitor, _, state, paper, lab, account, dataset, _, rows = harness(tmp_path)
    rules = {key: value for key, value in definition(direction).items() if key != "exit"}
    version = lab.create_strategy("Stop móvil GRVT", {
        **rules, "stop_loss": "0.2", "take_profit": "0.8", "trailing_stop": "0.05"})
    session = monitor.create(account["id"], version["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    opened = monitor.poll(session["id"])
    assert opened["actions"][0]["status"] == "EXECUTED"
    position_id = opened["actions"][0]["position_id"]
    entry_bucket = START + 6 * STEP
    with paper.connect() as db:
        stored_session = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session["id"],)).fetchone()
        position = db.execute("SELECT * FROM paper_perp_positions WHERE id=?", (position_id,)).fetchone()
        assert position["opened_at_ns"] // 1_000_000_000 // STEP * STEP == entry_bucket
        initial_level, initial_kind = monitor._stop_level(stored_session, position, version["definition"])
        assert initial_kind == "TRAILING_STOP"
        assert initial_level == Decimal(position["entry_price"]) * (
            Decimal("0.95") if direction == "LONG" else Decimal("1.05"))
    if direction == "LONG":
        # La primera vela que contiene la entrada no debe cerrar ya bajo el
        # stop inicial; su máximo tampoco se puede atribuir al post-fill.
        rows[6].update({"o": "100", "h": "102", "l": "99", "c": "100"})
    rows.append({"t": START + 7 * STEP, "o": bar[0], "h": bar[1],
                 "l": bar[2], "c": bar[3], "v": "17"})
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 7 * STEP + 10)
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 8 * STEP + 10)
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    restarted = SignalMonitor(paper.path, lab.path, adapter_factory=monitor.adapter_factory,
                              clock_ns=lambda: state["now"] * 1_000_000_000)
    assert not restarted._risk_stop_cache
    with paper.connect() as db:
        stored_session = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session["id"],)).fetchone()
        position = db.execute("SELECT * FROM paper_perp_positions WHERE id=?", (position_id,)).fetchone()
        level, kind = restarted._stop_level(stored_session, position, version["definition"])
        close = Decimal("113" if direction == "LONG" else "90")
        next_bar = Candle(START + 8 * STEP, close, close, close, close, Decimal("18"))
        archived_only_session = {**dict(stored_session), "candles_json": "not-needed-for-candle-decision"}
        assert restarted._decision(db, archived_only_session, next_bar, False, None,
                                   version["definition"])[1] == "TRAILING_STOP_AT_CANDLE_CLOSE"
    assert str(level) == expected_level and kind == "TRAILING_STOP"
    state["now"] += 1
    observed = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                         Decimal(mark), Decimal(mark) - 1, Decimal(mark) + 1)
    assert restarted.on_risk_quote(session["id"], observed) == "EXECUTED"
    assert restarted._risk_stop_cache
    assert restarted.detail(session["id"])["actions"][-1]["reason"] == "TRAILING_STOP_MARK_STREAM"
    assert paper.position(account["id"], position_id)["closed_at_ns"] == observed.event_time_ns
    assert restarted.verify_source(session["id"])["verified"]


def test_grvt_paper_trailing_stop_corrupt_history_halts_without_fill(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    version = lab.create_strategy("Stop móvil con histórico íntegro", {
        **definition(), "trailing_stop": "0.05"})
    session = monitor.create(account["id"], version["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    opened = monitor.poll(session["id"])
    position_id = opened["actions"][0]["position_id"]
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_sessions SET candles_json='[]' WHERE id=?", (session["id"],))
    state["now"] += 1
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      100, 99, 101)
    assert monitor.on_risk_quote(session["id"], quote) == "GAP"
    assert monitor.detail(session["id"])["status"] == "GAP"
    assert paper.position(account["id"], position_id)["closed_at_ns"] is None
    assert len(monitor.detail(session["id"])["actions"]) == 1


def test_grvt_risk_stop_cache_reuses_archive_but_tampering_still_halts(tmp_path, monkeypatch):
    from app.derivatives import signal_monitor as monitor_module

    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    version = lab.create_strategy("Stop archivado con caché", {
        **definition(), "trailing_stop": "0.05"})
    session = monitor.create(account["id"], version["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    position_id = monitor.poll(session["id"])["actions"][0]["position_id"]
    original_page = monitor_module.verified_archive_page
    page_calls = []

    def counted_page(*args, **kwargs):
        page_calls.append(1)
        return original_page(*args, **kwargs)

    monkeypatch.setattr(monitor_module, "verified_archive_page", counted_page)
    with paper.connect() as db:
        position = db.execute("SELECT * FROM paper_perp_positions WHERE id=?",
                              (position_id,)).fetchone()
        entry = Decimal(position["entry_price"])
    for tick in (1, 2):
        state["now"] += 1
        quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                          entry, entry - 1, entry + 1)
        assert monitor.on_risk_quote(session["id"], quote) == "OBSERVED"
        if tick == 1:
            assert page_calls
            calls_after_first = len(page_calls)
        else:
            assert len(page_calls) == calls_after_first
    last_t = monitor.detail(session["id"])["last_t"]
    with paper.connect() as db:
        db.execute("""UPDATE paper_signal_all_candles SET candle_json='{}'
                      WHERE session_id=? AND candle_t=?""",
                   (session["id"], last_t))
    state["now"] += 1
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      entry, entry - 1, entry + 1)
    assert monitor.on_risk_quote(session["id"], quote) == "GAP"
    assert paper.position(account["id"], position_id)["closed_at_ns"] is None


def test_grvt_paper_trailing_stop_does_not_override_margin_breach(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    version = lab.create_strategy("Stop móvil tras margen", {
        **definition(), "trailing_stop": "0.05"})
    session = monitor.create(account["id"], version["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    opened = monitor.poll(session["id"])
    position_id = opened["actions"][0]["position_id"]
    state["now"] += 1
    breach = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                       200, 199, 201)
    assert monitor.on_risk_quote(session["id"], breach) == "MARGIN_RISK"
    assert monitor.detail(session["id"])["status"] == "STOPPED"
    assert paper.position(account["id"], position_id)["closed_at_ns"] is None


def test_grvt_paper_trailing_pending_close_expires_without_late_fill(tmp_path, monkeypatch):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    rules = {key: value for key, value in definition().items() if key != "exit"}
    version = lab.create_strategy("Stop móvil pendiente", {
        **rules, "stop_loss": "0.2", "take_profit": "0.8", "trailing_stop": "0.05"})
    session = monitor.create(account["id"], version["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    opened = monitor.poll(session["id"])
    position_id = opened["actions"][0]["position_id"]

    def unavailable(*_args, **_kwargs):
        raise PaperPerpError("ticker no disponible")

    monkeypatch.setattr(monitor.paper, "close_position", unavailable)
    state["now"] += 1
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      104, 103, 105)
    with pytest.raises(SignalMonitorError, match="pendiente"):
        monitor.on_risk_quote(session["id"], quote)
    assert monitor.detail(session["id"])["actions"][-1]["reason"] == "TRAILING_STOP_MARK_STREAM"
    state["now"] += 21
    assert monitor._execute_pending(session["id"]) == "EXPIRED"
    assert monitor.detail(session["id"])["status"] == "GAP"
    assert monitor.detail(session["id"])["actions"][-1]["status"] == "EXPIRED"
    assert paper.position(account["id"], position_id)["closed_at_ns"] is None


def test_grvt_gap_analysis_reports_missed_signals_without_paper_actions(tmp_path):
    monitor, client, state, _, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    assert client.get(f"/paper/perp/signals/{session['id']}/gap-analysis").status_code == 422
    state["now"] = START + 7 * STEP + 10
    assert monitor.poll(session["id"])["poll_result"] == "GAP"
    before = monitor.detail(session["id"])
    response = client.get(f"/paper/perp/signals/{session['id']}/gap-analysis")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "RECONSTRUCTED_NO_EXECUTION"
    assert report["candles_reviewed"] == 2
    assert report["paper_state_unchanged"] is True
    assert monitor.gap_analyses(session["id"])[0]["hash"] == report["archive"]["hash"]
    assert monitor.detail(session["id"]) == before


def test_grvt_readiness_matches_create_freshness_without_creating_session(tmp_path):
    monitor, client, state, _, _, account, dataset, strategy, _ = harness(tmp_path)
    endpoint = f"/paper/perp/signals/readiness?dataset_id={dataset['id']}"
    assert client.get(endpoint).json()["ready"] is True
    assert monitor.sessions(account["id"]) == []
    state["now"] += STEP
    stale = client.get(endpoint)
    assert stale.status_code == 200 and stale.json()["ready"] is False
    assert "última vela cerrada" in stale.json()["reason"]
    with pytest.raises(SignalMonitorError, match="última vela cerrada"):
        monitor.create(account["id"], strategy["version_id"], dataset["id"])
    assert monitor.sessions(account["id"]) == []


def test_grvt_paper_higher_timeframe_waits_for_closed_candle(tmp_path):
    monitor, _, state, _, lab, account, dataset, _, all_rows = harness(tmp_path)
    rule = {"kind": "CLOSE_ABOVE_EMA", "period": 2, "timeframe": "4h"}
    version = lab.create_strategy("Filtro 4h", {**definition("LONG"), "entry": rule})
    session = monitor.create(account["id"], version["version_id"], dataset["id"])
    assert session["coverage"]["rules"][0]["complete_bars"] == 1
    assert session["coverage"]["rules"][0]["status"] == "INSUFFICIENT"
    state["now"] = START + 6 * STEP + 10
    observed = monitor.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    event = observed["observations"][-1]
    assert event["entry_match"] is False
    assert event["evidence"]["entry"]["higher_candle_t"] == START
    assert event["evidence"]["entry"]["threshold"] is None
    assert event["evidence"]["entry"] == evaluate(rule, validate_candles(all_rows[:6], "1h",
        now=state["now"])[0], 5, base_timeframe="1h")[1]
    assert monitor.detail(session["id"])["coverage"]["rules"][0]["complete_bars"] == 1
    assert observed["actions"] == []


def test_grvt_paper_rejects_unreachable_higher_timeframe_before_creating_session(tmp_path):
    monitor, _, _, _, lab, account, dataset, _, _ = harness(tmp_path)
    version = lab.create_strategy("EMA diaria imposible", {
        **definition("LONG"), "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 300,
                                           "timeframe": "1d"}})
    with pytest.raises(LabError, match="necesita 300 velas 1d completas"):
        monitor.create(account["id"], version["version_id"], dataset["id"],
                       "AUTO_PAPER", "1", "2")
    assert monitor.sessions(account["id"]) == []


def test_grvt_paper_source_api_verifies_prefix_and_distinguishes_unavailable_lab(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    app = FastAPI()
    app.include_router(create_signal_monitor_router(paper.path, lab.path, monitor=monitor))
    client = TestClient(app)
    endpoint = f"/paper/perp/signals/{session['id']}/verify"
    assert client.get(endpoint).json()["verified"] is True
    state["now"] = START + 6 * STEP + 10
    observed = monitor.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    assert client.get(endpoint).json()["verified"] is True
    before = monitor.detail(session["id"])
    with lab.connect() as db:
        db.execute("UPDATE datasets SET candles_json='[]' WHERE id=?", (dataset["id"],))
    result = client.get(endpoint)
    assert result.status_code == 200
    assert result.json()["status"] == "unverifiable"
    assert result.json()["verified"] is False
    assert monitor.detail(session["id"]) == before


def advance_with_continuous_risk_feed(monitor, session_id, state, target_second):
    """Una prueba de velas AUTO no puede fingir una hora sin ticker."""
    while state["now"] + 10 < target_second:
        state["now"] += 10
        quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                          100, 99, 101)
        assert monitor.on_risk_quote(session_id, quote) == "OBSERVED"
    state["now"] = target_second


def test_grvt_paper_encryption_retains_auto_session_and_restores_backup(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    assert monitor.poll(session["id"])["actions"][0]["status"] == "EXECUTED"
    before = verify_paper_perp_database(paper.path)
    assert before["counts"]["paper_signal_actions"] == 1
    passphrase = "a local test password 123"
    result = encrypt_active_paper_perp(paper, "CIFRAR GRVT PAPER", passphrase)
    assert result["verified"] == before
    assert paper_perp_format(paper.path) == "encrypted_or_invalid"
    with pytest.raises(PaperPerpError, match="bloqueado"):
        paper.positions(account["id"])
    app = FastAPI()
    app.include_router(create_paper_perp_router(paper.path))
    app.include_router(create_signal_monitor_router(paper.path, lab.path))
    client = TestClient(app)
    assert client.get("/paper/perp/status").json()["unlocked"] is False
    assert client.get("/paper/perp/accounts").status_code == 423
    assert client.get(f"/paper/perp/signals/{session['id']}").status_code == 423
    assert client.post("/paper/perp/unlock", json={"passphrase": "wrong password for test"}).status_code == 422
    assert client.post("/paper/perp/unlock", json={"passphrase": passphrase}).status_code == 200
    assert client.get("/paper/perp/accounts").json()[0]["id"] == account["id"]
    assert client.get(f"/paper/perp/signals/{session['id']}").json()["actions"][0]["status"] == "EXECUTED"
    assert verify_paper_perp_database(paper.path, passphrase) == before
    paper.create_account("Nueva", "1000", "500", "800", "5", 2, "0.001", "0")
    paper_perp_access.lock(paper.path)
    restored = restore_secure_paper_perp(paper, result["backup"], "RESTAURAR GRVT PAPER", passphrase)
    assert restored["previous_backup"] is not None
    assert client.get("/paper/perp/accounts").status_code == 423
    paper_perp_access.unlock(paper.path, passphrase)
    assert [row["id"] for row in paper.accounts()] == [account["id"]]
    assert monitor.detail(session["id"])["actions"][0]["status"] == "EXECUTED"
    paper_perp_access.lock(paper.path)


def test_grvt_testnet_bridge_binds_recent_short_paper_entry(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    observed = monitor.poll(session["id"])
    assert observed["actions"][0]["status"] == "EXECUTED"
    with paper.connect() as db:
        request_id = db.execute("SELECT request_id FROM paper_signal_actions WHERE session_id=?",
                                (session["id"],)).fetchone()["request_id"]
    now_ns = state["now"] * 1_000_000_000
    limits = GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("1000"))
    order = {
        "sub_account_id": "123456", "is_market": False,
        "time_in_force": "IMMEDIATE_OR_CANCEL", "post_only": False,
        "reduce_only": False,
        "legs": [{"instrument": "BTC_USDT_Perp", "size": "1", "limit_price": "100",
                  "is_buying_asset": False}],
        "signature": {"signer": "0x" + "a" * 40, "r": "0x" + "1" * 64,
                      "s": "0x" + "2" * 64, "v": 27,
                      "expiration": str(now_ns + 60_000_000_000), "nonce": 1,
                      "chain_id": "326"},
        "metadata": {"client_order_id": client_id_for_signal(session["id"], request_id)},
        "builder": "0x" + "a" * 40, "builder_fee": "0",
    }
    calls = []

    def fake_submit(vault, client, candidate, policy, *, clock_ns, signal_source):
        assert signal_source["action"] == "OPEN"
        assert signal_source["direction"] == "SHORT"
        assert signal_source["client_order_id"] == candidate["metadata"]["client_order_id"]
        calls.append(candidate["metadata"]["client_order_id"])
        return "TESTNET_JOURNAL_CLAIMED"

    def submit(candidate=order, *, clock=lambda: state["now"] * 1_000_000_000,
               verifier=lambda *args, **kwargs: None):
        return submit_signal_testnet_once(paper, lab, None, None, candidate, limits,
                                          session_id=session["id"], action_request_id=request_id,
                                          clock_ns=clock, submitter=fake_submit,
                                          signature_verifier=verifier)

    binding, result = submit()
    assert binding.action == "OPEN"
    assert binding.version_id == strategy["version_id"]
    assert binding.dataset_hash == dataset["hash"]
    assert result == "TESTNET_JOURNAL_CLAIMED"
    assert calls == [client_id_for_signal(session["id"], request_id)]
    assert calls[0] == client_id_for_signal(session["id"], request_id)
    def reject_signature(candidate, policy, *, now_ns):
        assert candidate is order and policy is limits and now_ns == state["now"] * 1_000_000_000
        raise GrvtTestnetSignError("Firma testnet inválida")
    with pytest.raises(GrvtTestnetSignError, match="inválida"):
        submit(verifier=reject_signature)
    assert len(calls) == 1

    bad_side = json.loads(json.dumps(order))
    bad_side["legs"][0]["is_buying_asset"] = True
    with pytest.raises(GrvtTestnetSignalError, match="Lado"):
        submit(bad_side)
    bad_id = json.loads(json.dumps(order))
    bad_id["metadata"]["client_order_id"] = str(2**63 + 7)
    with pytest.raises(GrvtTestnetSignalError, match="ID estable"):
        submit(bad_id)
    with pytest.raises(GrvtTestnetSignalError, match="reciente"):
        submit(clock=lambda: now_ns + 21_000_000_000)
    assert len(calls) == 1
    clock_ticks = iter((now_ns, now_ns + 21_000_000_000))
    with pytest.raises(GrvtTestnetSignalError, match="venció"):
        submit_signal_testnet_once(
            paper, lab, None, None, order, limits, session_id=session["id"],
            action_request_id=request_id, clock_ns=lambda: next(clock_ticks),
            submitter=lambda *args, **kwargs: kwargs["clock_ns"](),
            signature_verifier=lambda *args, **kwargs: None,
        )
    with paper.connect() as db:
        original = db.execute("""SELECT entry_match,evidence_json FROM paper_signal_observations
                                 WHERE session_id=?""", (session["id"],)).fetchone()
        db.execute("UPDATE paper_signal_observations SET entry_match=1-entry_match WHERE session_id=?",
                   (session["id"],))
    with pytest.raises(GrvtTestnetSignalError, match="Señales PAPER no reproducibles"):
        submit()
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_observations SET entry_match=?,evidence_json=? WHERE session_id=?",
                   (original["entry_match"], original["evidence_json"], session["id"]))
        altered = json.loads(original["evidence_json"])
        altered["entry"] = {"altered": True}
        db.execute("UPDATE paper_signal_observations SET evidence_json=? WHERE session_id=?",
                   (json.dumps(altered), session["id"]))
    with pytest.raises(GrvtTestnetSignalError, match="Señales PAPER no reproducibles"):
        submit()
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_observations SET evidence_json=? WHERE session_id=?",
                   (original["evidence_json"], session["id"]))
        db.execute("UPDATE paper_signal_sessions SET engine_version='lab-1.12' WHERE id=?",
                   (session["id"],))
    with pytest.raises(GrvtTestnetSignalError, match="Señales PAPER no reproducibles"):
        submit()
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_sessions SET engine_version=? WHERE id=?",
                   (session["engine_version"], session["id"]))
    assert len(calls) == 1
    monitor.stop(session["id"])
    with pytest.raises(GrvtTestnetSignalError, match="detenida"):
        submit()
    assert len(calls) == 1


def test_ephemeral_testnet_signing_replays_real_paper_action_before_send(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    observed = monitor.poll(session["id"])
    assert observed["actions"][0]["status"] == "EXECUTED"
    with paper.connect() as db:
        action_id = db.execute(
            "SELECT request_id FROM paper_signal_actions WHERE session_id=?",
            (session["id"],),
        ).fetchone()["request_id"]
    key = bytes.fromhex("33" * 32)  # Clave sintética exclusiva del test.
    signer = Account.from_key(key).address
    limits = GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("100"))
    instrument = GrvtTestnetInstrument(
        "BTC_USDT_Perp", 123456789, 3,
        Decimal("0.1"), Decimal("0.001"), Decimal("10"),
    )
    sent = []

    def fake_send(vault, client, order, policy, *, clock_ns, signal_source,
                  pre_claim_guard):
        assert signal_source["action"] == "OPEN"
        assert signal_source["direction"] == "SHORT"
        assert order["signature"]["signer"] == signer
        assert order["legs"][0]["is_buying_asset"] is False
        assert order["metadata"]["client_order_id"] == client_id_for_signal(
            session["id"], action_id)
        pre_claim_guard()
        sent.append(order)
        return "JOURNAL_CLAIMED"

    def attempt():
        return sign_and_submit_signal_testnet_once(
            paper, lab, None, lambda api_key: object(), "dedicated-testnet-key",
            limits, session_id=session["id"], action_request_id=action_id,
            dedicated_key_hex="0x" + key.hex(), expected_signer=signer,
            builder="0x" + "a" * 40, side="SELL", size=Decimal("1"),
            limit_price=Decimal("100"), reduce_only=False,
            clock_ns=lambda: state["now"] * 1_000_000_000,
            instrument_fetcher=lambda name: instrument,
            quote_fetcher=lambda name: GrvtTestnetQuote(
                "BTC_USDT_Perp", state["now"] * 1_000_000_000,
                state["now"] * 1_000_000_000,
                Decimal("100"), Decimal("100.01"), Decimal("1"), Decimal("1"),
            ),
            signal_submitter=lambda *args, **kwargs: submit_signal_testnet_once(
                *args, **kwargs, submitter=fake_send,
            ),
            submission_guard=lambda *_: None,
        )

    binding, result = attempt()
    assert binding.action == "OPEN" and result == "JOURNAL_CLAIMED"
    assert len(sent) == 1 and key.hex() not in str(sent[0])
    monitor.stop(session["id"])
    with pytest.raises(GrvtTestnetSignalError, match="detenida"):
        attempt()
    assert len(sent) == 1


def test_grvt_testnet_bridge_requires_as_settled_paper_close_and_lab_origin(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    strategy = lab.create_strategy("Long causal", definition("LONG"))
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    opened = monitor.poll(session["id"])
    assert opened["actions"][0]["status"] == "EXECUTED"
    state["now"] += 10
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      Decimal("110"), Decimal("109"), Decimal("111"))
    assert monitor.on_risk_quote(session["id"], quote) == "EXECUTED"
    with paper.connect() as db:
        close = db.execute("""SELECT request_id FROM paper_signal_actions
                              WHERE session_id=? AND action='CLOSE'""",
                           (session["id"],)).fetchone()
    assert close is not None
    request_id = close["request_id"]
    now_ns = state["now"] * 1_000_000_000
    limits = GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("1000"))
    order = {
        "sub_account_id": "123456", "is_market": False,
        "time_in_force": "IMMEDIATE_OR_CANCEL", "post_only": False,
        "reduce_only": True,
        "legs": [{"instrument": "BTC_USDT_Perp", "size": "1", "limit_price": "100",
                  "is_buying_asset": False}],
        "signature": {"signer": "0x" + "a" * 40, "r": "0x" + "1" * 64,
                      "s": "0x" + "2" * 64, "v": 27,
                      "expiration": str(now_ns + 60_000_000_000), "nonce": 1,
                      "chain_id": "326"},
        "metadata": {"client_order_id": client_id_for_signal(session["id"], request_id)},
        "builder": "0x" + "a" * 40, "builder_fee": "0",
    }
    calls = []

    def submitter(*args, **kwargs):
        assert kwargs["signal_source"]["action"] == "CLOSE"
        assert kwargs["signal_source"]["direction"] == "LONG"
        calls.append(True)
        return "CLAIMED"

    def submit():
        return submit_signal_testnet_once(paper, lab, None, None, order, limits,
                                          session_id=session["id"], action_request_id=request_id,
                                          clock_ns=lambda: now_ns, submitter=submitter,
                                          signature_verifier=lambda *args, **kwargs: None)

    binding, _ = submit()
    assert binding.action == "CLOSE"
    assert calls == [True]
    with paper.connect() as db:
        db.execute("UPDATE paper_perp_events SET kind='OBSERVE' WHERE request_id=?",
                   (request_id,))
    with pytest.raises(GrvtTestnetSignalError, match="Asiento"):
        submit()
    with paper.connect() as db:
        db.execute("UPDATE paper_perp_events SET kind='CLOSE' WHERE request_id=?",
                   (request_id,))
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_sessions SET dataset_hash=? WHERE id=?",
                   ("0" * 64, session["id"]))
    with pytest.raises(GrvtTestnetSignalError, match="Procedencia"):
        submit()
    assert calls == [True]


def test_comparison_links_exact_lab_run_without_mixing_paper_returns(tmp_path):
    monitor, client, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    endpoint = f"/paper/perp/signals/{session['id']}/comparison"
    missing = client.get(endpoint)
    assert missing.status_code == 200
    assert missing.json()["historical"] is None
    assert missing.json()["paper"]["realized_pnl_usdt"] == "0"
    assert missing.json()["signal_replay"]["status"] == "no_observations"
    assert missing.json()["signal_replay"]["checked_observations"] == 0
    _, raw = lab.dataset(dataset["id"])
    bars, _ = validate_candles(raw, "1h", now=state["now"])
    saved = lab.save_run(dataset["id"], strategy["version_id"],
                         run_backtest(bars, validate_strategy(definition()), "1h", engine_version="lab-1.5"), "lab-1.5")
    compared = client.get(endpoint).json()
    assert compared["historical"]["run_id"] == saved["id"]
    assert compared["historical"]["metrics"] == saved["metrics"]
    assert compared["comparison_basis"] == "NOT_DIRECTLY_COMPARABLE"
    assert "return_pct" not in compared["paper"]
    state["now"] = START + 6 * STEP + 1
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    current = client.get(endpoint).json()
    assert current["paper"]["observed_candles"] == 1
    assert current["paper"]["executed_opens"] == 1
    assert current["paper"]["open_positions"] == 1
    assert current["paper"]["realized_pnl_usdt"] == "0"
    assert current["historical"]["run_id"] == saved["id"]
    assert current["signal_replay"]["verified"] is True
    assert current["signal_replay"]["checked_observations"] == 1
    assert current["signal_frequency"]["paper"]["evaluated_candles"] == 1
    assert current["signal_frequency"]["paper"]["first_candle_t"] == current["signal_frequency"]["paper"]["last_candle_t"]
    assert current["signal_frequency"]["paper"]["signal_bursts"] == 1
    assert current["signal_frequency"]["historical"]["evaluated_candles"] == len(saved["equity"])
    assert len(saved["events"]) == 3
    assert current["signal_frequency"]["historical"]["signal_bursts"] == 1
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_observations SET entry_match=1-entry_match WHERE session_id=?",
                   (session["id"],))
    altered = client.get(endpoint).json()["signal_replay"]
    assert altered["status"] == "mismatch" and altered["first_mismatch_t"] is not None
    assert client.get(endpoint).json()["signal_frequency"]["paper"] is None
    assert client.get(endpoint, headers={"Origin": "https://outside.invalid"}).status_code == 403
    assert client.get("/paper/perp/signals/missing/comparison").status_code == 404


def test_grvt_comparison_recovers_lab_reference_after_seed_refresh(tmp_path):
    monitor, _, state, _, lab, account, parent, strategy, _ = harness(tmp_path)
    _, parent_rows = lab.dataset(parent["id"])
    parent_bars, _ = validate_candles(parent_rows, "1h", now=state["now"])
    run = lab.save_run(parent["id"], strategy["version_id"],
                       run_backtest(parent_bars, validate_strategy(definition()), "1h",
                                    engine_version="lab-1.5"), "lab-1.5")
    changed = [dict(row) for row in parent_rows]
    changed[0]["v"] = "99"
    fresh_bars, _ = validate_candles(changed, "1h", now=state["now"])
    child = lab.save_refreshed_dataset(parent["id"], parent["hash"],
                                       parent["symbol"], parent["timeframe"],
                                       parent["source"], fresh_bars)
    session = monitor.create(account["id"], strategy["version_id"], child["id"],
                             "AUTO_PAPER", "1", "2")
    compared = monitor.comparison(session["id"])
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
    monitor.stop(session["id"])
    later = monitor.create(account["id"], strategy["version_id"], grandchild["id"],
                           "AUTO_PAPER", "1", "2")
    later_comparison = monitor.comparison(later["id"])
    assert later_comparison["historical"]["run_id"] == run["id"]
    assert later_comparison["historical"]["refresh_hops"] == 2


def test_grvt_paper_pins_verified_lab_run_after_seed_refresh(tmp_path):
    monitor, _, state, _, lab, account, parent, strategy, _ = harness(tmp_path)
    _, raw = lab.dataset(parent["id"])
    bars, _ = validate_candles(raw, "1h", now=state["now"])
    result = run_backtest(bars, strategy["definition"], "1h", engine_version="lab-1.5")
    origin = lab.save_run(parent["id"], strategy["version_id"], result, "lab-1.5")
    changed = [dict(row) for row in raw]
    changed[0]["v"] = "99"
    fresh_bars, _ = validate_candles(changed, "1h", now=state["now"])
    child = lab.save_refreshed_dataset(parent["id"], parent["hash"], parent["symbol"],
                                       parent["timeframe"], parent["source"], fresh_bars)
    session = monitor.create(account["id"], strategy["version_id"], child["id"],
                             source_run_id=origin["id"])
    assert session["source_run_id"] == origin["id"]
    nearer = lab.save_run(child["id"], strategy["version_id"],
                          run_backtest(fresh_bars, strategy["definition"], "1h", engine_version="lab-1.5"),
                          "lab-1.5")
    comparison = monitor.comparison(session["id"])
    assert comparison["historical"]["run_id"] == origin["id"] != nearer["id"]
    assert comparison["historical"]["dataset_relation"] == "REFRESHED_FROM"
    with lab.connect() as db:
        db.execute("UPDATE dataset_refreshes SET parent_hash=? WHERE child_id=?",
                   ("0" * 64, child["id"]))
    changed = monitor.comparison(session["id"])
    assert changed["historical"]["run_id"] == origin["id"]
    assert changed["historical"]["verification"]["verified"] is False
    assert changed["historical"]["metrics"] is None


def test_comparison_separates_provisional_and_funding_covered_closed_pnl(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _rows = harness(tmp_path)
    rules = {key: value for key, value in definition().items() if key != "exit"}
    strategy = lab.create_strategy("Cierre observado", validate_strategy({
        **rules, "stop_loss": "0.9", "take_profit": "0.9", "max_holding_bars": 1}))
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    opened = monitor.poll(session["id"])
    assert opened["actions"][0]["status"] == "EXECUTED"
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 7 * STEP + 10)
    closed = monitor.poll(session["id"])
    assert closed["actions"][1]["status"] == "EXECUTED"
    position_id = opened["actions"][0]["position_id"]
    position = paper.position(account["id"], position_id)
    assert position["funding_coverage"] == "UNASSESSED"
    provisional = monitor.comparison(session["id"])["paper"]
    assert provisional["funding_incomplete_closed"] == 1
    assert provisional["funding_covered_closed"] == 0
    assert provisional["realized_pnl_provisional_usdt"] == position["realized_pnl"]
    assert provisional["realized_pnl_funding_covered_usdt"] == "0"
    assert provisional["realized_pnl_usdt"] == position["realized_pnl"]

    # La clasificación cambia sólo cuando la cobertura llega al cierre;
    # no se inventa un pago privado GRVT ni se altera el P/L de esta prueba.
    with paper.connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_checked_through_ns=? WHERE id=?",
                   (position["closed_at_ns"], position_id))
        db.execute("INSERT INTO paper_perp_funding_anchors VALUES (?,?,?,?,?,?,?)",
                   (position_id, position["opened_at_ns"] - 1, "0.01", "100", 8,
                    "test-fixture", "2026-10-24T00:00:00+00:00"))
    covered = monitor.comparison(session["id"])["paper"]
    assert covered["funding_incomplete_closed"] == 0
    assert covered["funding_covered_closed"] == 1
    assert covered["realized_pnl_funding_covered_usdt"] == position["realized_pnl"]
    assert covered["realized_pnl_provisional_usdt"] == "0"
    assert covered["realized_pnl_usdt"] == provisional["realized_pnl_usdt"]


@pytest.mark.parametrize("changed,status", [("result", "mismatch"), ("strategy", "mismatch"),
                                           ("dataset", "mismatch"),
                                           ("engine", "engine_unavailable"),
                                           ("legacy", "legacy_unpinned")])
def test_comparison_hides_unverified_historical_metrics(tmp_path, changed, status):
    monitor, client, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    _, raw = lab.dataset(dataset["id"])
    bars, _ = validate_candles(raw, "1h", now=state["now"])
    saved = lab.save_run(dataset["id"], strategy["version_id"],
                         run_backtest(bars, strategy["definition"], "1h", engine_version="lab-1.5"), "lab-1.5")
    endpoint = f"/paper/perp/signals/{session['id']}/comparison"
    before = client.get(endpoint).json()
    assert before["historical"]["verification"]["status"] == "verified"
    assert before["historical"]["metrics"] == saved["metrics"]
    with sqlite3.connect(lab.path) as db:
        if changed == "result":
            result = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (saved["id"],)).fetchone()[0])
            result["metrics"]["net_profit"] = "999999"
            db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(result), saved["id"]))
        elif changed == "strategy":
            revised = {**strategy["definition"], "take_profit": "0.3"}
            db.execute("UPDATE strategy_versions SET definition_json=? WHERE id=?",
                       (json.dumps(revised), strategy["version_id"]))
        elif changed == "dataset":
            db.execute("UPDATE datasets SET candles_json='[]' WHERE id=?", (dataset["id"],))
        elif changed == "engine":
            db.execute("UPDATE runs SET engine_version='lab-0.0' WHERE id=?", (saved["id"],))
        else:
            result = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (saved["id"],)).fetchone()[0])
            result.pop("strategy_hash")
            result.pop("result_hash")
            db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(result), saved["id"]))
    compared = client.get(endpoint).json()
    assert compared["historical"]["verification"]["status"] == status
    assert compared["historical"]["metrics"] is None
    assert compared["paper"] == before["paper"]


@pytest.mark.parametrize("engine_version", ["lab-1.5", "lab-1.6"])
def test_grvt_paper_legacy_session_continues_with_unchanged_rules(tmp_path, engine_version):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    # Una sesión anterior a v10 carecía de instantánea incremental.
    with sqlite3.connect(paper.path) as db:
        db.execute("UPDATE paper_signal_sessions SET engine_version=? WHERE id=?",
                   (engine_version, session["id"]))
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("PRAGMA user_version=9")
    state["now"] = START + 6 * STEP + 1
    observed = monitor.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    assert observed["status"] == "ACTIVE"


def test_auto_paper_opens_and_closes_once_from_causal_rules(tmp_path):
    monitor, client, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    body = {
        "account_id": account["id"], "version_id": strategy["version_id"],
        "dataset_id": dataset["id"], "execution_mode": "AUTO_PAPER",
        "size": "1", "leverage": "2"}
    assert client.post("/paper/perp/signals", json=body).status_code == 503  # sin worker
    assert client.post("/paper/perp/signals", json=body | {"size": 1}).status_code == 422
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    assert session["execution_mode"] == "AUTO_PAPER"
    with pytest.raises(SignalMonitorError, match="sesión automática activa"):
        monitor.create(account["id"], strategy["version_id"], dataset["id"],
                       "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    first = monitor.poll(session["id"])
    assert first["poll_result"] == "OBSERVED"
    assert first["actions"][0]["status"] == "EXECUTED"
    position = paper.positions(account["id"])[0]
    assert position["mode"] == "PAPER_AUTO" and position["origin_session_id"] == session["id"]
    assert position["side"] == "SHORT" and position["closed_at_ns"] is None
    assert monitor.poll(session["id"])["poll_result"] == "WAIT"
    assert len(paper.positions(account["id"])) == 1
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 7 * STEP + 1)
    closed = monitor.poll(session["id"])
    assert closed["poll_result"] == "OBSERVED"
    assert [action["action"] for action in closed["actions"]] == ["OPEN", "CLOSE"]
    assert all(action["status"] == "EXECUTED" for action in closed["actions"])
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is not None
    assert monitor.poll(session["id"])["poll_result"] == "WAIT"
    assert len([event for event in paper.events(account["id"]) if event["kind"] in ("OPEN", "CLOSE")]) == 2


def test_auto_paper_pending_recovers_only_inside_observation_window(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"], "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    state["ticker_offline"] = True
    with pytest.raises(SignalMonitorError, match="Datos GRVT"):
        monitor.poll(session["id"])
    detail = monitor.detail(session["id"])
    assert len(detail["observations"]) == 1 and detail["actions"][0]["status"] == "PENDING"
    assert paper.account(account["id"])["open_positions"] == 0
    state["ticker_offline"] = False
    recovered = SignalWorker(SignalMonitor(paper.path, lab.path,
                                           adapter_factory=monitor.adapter_factory,
                                           clock_ns=monitor.clock_ns)).run_once()
    assert recovered[session["id"]] == "EXECUTED"
    assert paper.account(account["id"])["open_positions"] == 1
    assert monitor.detail(session["id"])["actions"][0]["status"] == "EXECUTED"

    second = paper.create_account("Otro", "1000", "500", "800", "5", 2, "0.001", "0")
    state["now"] = START + 5 * STEP + 10
    late = monitor.create(second["id"], strategy["version_id"], dataset["id"], "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    state["ticker_offline"] = True
    with pytest.raises(SignalMonitorError):
        monitor.poll(late["id"])
    state["now"] = START + 7 * STEP
    assert monitor.poll(late["id"])["poll_result"] == "GAP"
    assert monitor.detail(late["id"])["actions"][0]["status"] == "EXPIRED"
    assert paper.account(second["id"])["open_positions"] == 0


def test_auto_paper_pending_expires_if_indicator_state_diverges(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    state["ticker_offline"] = True
    with pytest.raises(SignalMonitorError, match="Datos GRVT"):
        monitor.poll(session["id"])
    assert monitor.detail(session["id"])["actions"][0]["status"] == "PENDING"
    with paper.connect() as db:
        db.execute("""UPDATE paper_signal_sessions
                      SET indicator_state_json=json_set(indicator_state_json,'$.count',999)
                      WHERE id=?""", (session["id"],))
    state["ticker_offline"] = False
    result = monitor.poll(session["id"])
    assert result["poll_result"] == "GAP" and result["status"] == "GAP"
    assert result["actions"][0]["status"] == "EXPIRED"
    assert paper.account(account["id"])["open_positions"] == 0


def pending_rule_close(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 7 * STEP + 1)
    state["ticker_offline"] = True
    with pytest.raises(SignalMonitorError, match="Datos GRVT"):
        monitor.poll(session["id"])
    pending = monitor.detail(session["id"])["actions"][-1]
    assert pending["action"] == "CLOSE" and pending["status"] == "PENDING"
    return monitor, state, paper, account, session, position


def test_pending_rule_close_uses_only_stream_quotes_from_candle_close(tmp_path):
    monitor, state, paper, account, session, position = pending_rule_close(tmp_path)
    boundary_ns = (START + 7 * STEP) * 1_000_000_000
    early = PerpQuote("BTC_USDT_Perp", boundary_ns - 1, 100, 98, 102)
    assert monitor.on_risk_quote(session["id"], early) == "ACTION_ALREADY_PENDING"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None
    assert monitor.detail(session["id"])["actions"][-1]["status"] == "PENDING"
    state["now"] += 1
    fresh = PerpQuote("BTC_USDT_Perp", boundary_ns, 100, 98, 103)
    assert monitor.on_risk_quote(session["id"], fresh) == "EXECUTED"
    closed = paper.position(account["id"], position["id"])
    assert closed["exit_price"] == "103" and closed["closed_at_ns"] == boundary_ns
    assert monitor.detail(session["id"])["actions"][-1]["status"] == "EXECUTED"
    assert monitor.poll(session["id"])["poll_result"] == "WAIT"
    assert len([event for event in paper.events(account["id"]) if event["kind"] == "CLOSE"]) == 1


def test_pending_rule_close_retries_if_stream_supersedes_rest_quote(tmp_path, monkeypatch):
    monitor, state, paper, account, session, position = pending_rule_close(tmp_path)
    state["ticker_offline"] = False
    state["ticker_event_ns"] = state["now"] * 1_000_000_000
    close_position = monitor.paper.close_position

    def close_after_risk_update(*args, **kwargs):
        # The risk transaction committed while REST was on its way to the ledger.
        with paper.connect() as db:
            db.execute("UPDATE paper_signal_sessions SET risk_last_quote_ns=? WHERE id=?",
                       (state["ticker_event_ns"] + 1, session["id"]))
        return close_position(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(monitor.paper, "close_position", close_after_risk_update)
        assert monitor.poll(session["id"])["poll_result"] == "WAIT_QUOTE"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None
    assert monitor.detail(session["id"])["actions"][-1]["status"] == "PENDING"
    state["now"] += 1
    fresh = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000, 100, 98, 102)
    assert monitor.on_risk_quote(session["id"], fresh) == "EXECUTED"
    assert len([event for event in paper.events(account["id"]) if event["kind"] == "CLOSE"]) == 1


@pytest.mark.parametrize("path", ["STREAM", "REST"])
def test_pending_rule_close_cannot_commit_after_execution_window(tmp_path, monkeypatch, path):
    monitor, state, paper, account, session, position = pending_rule_close(tmp_path)
    deadline = START + 8 * STEP
    state["now"] = deadline - 1
    state["ticker_offline"] = False
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_sessions SET risk_last_quote_ns=? WHERE id=?",
                   ((deadline - 2) * 1_000_000_000, session["id"]))
    close_position = monitor.paper.close_position

    def close_after_deadline(*args, **kwargs):
        state["now"] = deadline
        return close_position(*args, **kwargs)

    monkeypatch.setattr(monitor.paper, "close_position", close_after_deadline)
    if path == "STREAM":
        quote = PerpQuote("BTC_USDT_Perp", (deadline - 1) * 1_000_000_000, 100, 98, 102)
        assert monitor.on_risk_quote(session["id"], quote) == "GAP"
    else:
        assert monitor.poll(session["id"])["poll_result"] == "GAP"
    detail = monitor.detail(session["id"])
    assert detail["status"] == "GAP" and detail["actions"][-1]["status"] == "EXPIRED"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None
    assert not [event for event in paper.events(account["id"]) if event["kind"] == "CLOSE"]


def test_auto_paper_closed_candle_stop_and_account_limits(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    long = lab.create_strategy("Long stop", definition("LONG"))
    session = monitor.create(account["id"], long["version_id"], dataset["id"], "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    assert monitor.poll(session["id"])["actions"][0]["status"] == "EXECUTED"
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 7 * STEP + 1)
    second = monitor.poll(session["id"])
    assert second["actions"][1]["reason"] == "STOP_AT_CANDLE_CLOSE"
    assert second["actions"][1]["status"] == "EXECUTED"
    assert paper.account(account["id"])["open_positions"] == 0

    another = paper.create_account("Límite", "1000", "500", "800", "5", 2, "0.001", "0")
    state["now"] = START + 5 * STEP + 10
    limited = monitor.create(another["id"], long["version_id"], dataset["id"], "AUTO_PAPER", "10", "2")
    state["now"] = START + 6 * STEP + 1
    rejected = monitor.poll(limited["id"])
    assert rejected["actions"][0]["status"] == "REJECTED"
    assert rejected["status"] == "STOPPED"
    assert paper.account(another["id"])["open_positions"] == 0


def test_auto_paper_stream_stop_uses_mark_and_same_bid_ask_tick(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    assert position["side"] == "SHORT" and position["closed_at_ns"] is None
    state["now"] += 1
    quiet = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      100, 99, 101)
    assert monitor.on_risk_quote(session["id"], quiet) == "OBSERVED"
    assert monitor.on_risk_quote(session["id"], quiet) == "OLD_QUOTE"
    state["now"] += 1
    stop = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                     104, 103, 105)
    assert monitor.on_risk_quote(session["id"], stop) == "EXECUTED"
    closed = paper.position(account["id"], position["id"])
    assert closed["closed_at_ns"] == stop.event_time_ns
    assert closed["exit_mark"] == "104" and closed["exit_price"] == "105"
    actions = monitor.detail(session["id"])["actions"]
    assert [row["reason"] for row in actions] == ["ENTRY_RULE_AT_CANDLE_CLOSE", "STOP_MARK_STREAM"]
    assert all(row["status"] == "EXECUTED" for row in actions)
    assert monitor.on_risk_quote(session["id"], stop) == "NO_POSITION"
    state["now"] = START + 7 * STEP + 1
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    assert len(monitor.detail(session["id"])["actions"]) == 2  # sin reapertura en vela del stop
    assert paper.account(account["id"])["open_positions"] == 0


def test_auto_paper_margin_breach_halts_before_fictional_stop_fill(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    state["now"] += 1
    crash = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      Decimal("200"), Decimal("199"), Decimal("201"))
    assert monitor.on_risk_quote(session["id"], crash) == "MARGIN_RISK"
    detail = monitor.detail(session["id"])
    assert detail["status"] == "STOPPED"
    assert "no se simula liquidación" in detail["status_reason"]
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None
    assert [row["action"] for row in detail["actions"]] == ["OPEN"]
    risk = [row for row in paper.events(account["id"]) if row["kind"] == "OBSERVE"][-1]["details"]
    assert risk["risk_type"] == "CURRENT_RULES_MAINTENANCE_BREACH"
    assert Decimal(risk["isolated_equity"]) <= Decimal(risk["maintenance_requirement"])
    assert risk["liquidation_status"] == "NOT_DETERMINED"
    assert monitor.on_risk_quote(session["id"], crash) == "INACTIVE"
    assert not [row for row in paper.events(account["id"]) if row["kind"] == "CLOSE"]


def test_long_auto_paper_margin_breach_also_stays_open_for_review(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    long_version = lab.create_strategy("Long en PAPER", definition("LONG"))
    session = monitor.create(account["id"], long_version["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    assert position["side"] == "LONG"
    state["now"] += 1
    fall = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                     Decimal("1"), Decimal("0.9"), Decimal("1.1"))
    assert monitor.on_risk_quote(session["id"], fall) == "MARGIN_RISK"
    assert monitor.detail(session["id"])["status"] == "STOPPED"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None


def test_auto_paper_margin_uses_recorded_funding_and_fails_closed_without_current_rules(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    with paper.connect() as db:
        funding_time = position["opened_at_ns"] + 1
        db.execute("UPDATE paper_perp_positions SET funding_paid='49',funding_checked_through_ns=? WHERE id=?",
                   (funding_time, position["id"]))
        db.execute("INSERT INTO paper_perp_funding_anchors VALUES (?,?,?,?,?,?,?)",
                   (position["id"], funding_time - HOUR_NS, "0.01", "100", 1,
                    "test-fixture", "2026-10-24T00:00:00+00:00"))
        db.execute("""INSERT INTO paper_perp_funding_events VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   ("funding-fixture-49", account["id"], position["id"], "request-fixture-49",
                    funding_time, "-49", "100", 1, "49", "test-fixture",
                    "2026-10-23T00:00:00+00:00"))
    state["now"] += 1
    quiet = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      Decimal("100"), Decimal("99"), Decimal("101"))
    assert monitor.on_risk_quote(session["id"], quiet) == "MARGIN_RISK"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None

    other = paper.create_account("Otras reglas", "1000", "500", "800", "5", 2, "0.001", "0")
    state["now"] = START + 5 * STEP + 10
    second = monitor.create(other["id"], strategy["version_id"], dataset["id"],
                            "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(second["id"])
    second_position = paper.positions(other["id"])[0]
    state["rules_offline"] = True
    state["now"] += 1
    assert monitor.on_risk_quote(second["id"], quiet) == "MARGIN_UNVERIFIABLE"
    assert monitor.detail(second["id"])["status"] == "STOPPED"
    assert paper.position(other["id"], second_position["id"])["closed_at_ns"] is None
    assert [row for row in paper.events(other["id"]) if row["kind"] == "OBSERVE"][-1]["details"]["risk_type"] == "CURRENT_RULES_UNVERIFIABLE"


def test_current_margin_rules_override_opening_snapshot(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    assert position["margin_rules_at_ns"] == state["now"] * 1_000_000_000
    state["rules_maintenance"] = "0.99"
    state["rules_initial"] = "1"
    state["now"] += 1
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      Decimal("100"), Decimal("99"), Decimal("101"))
    assert monitor.on_risk_quote(session["id"], quote) == "MARGIN_RISK"
    risk = [row for row in paper.events(account["id"]) if row["kind"] == "OBSERVE"][-1]["details"]
    assert risk["rules_fetched_at_ns"] == quote.event_time_ns
    assert risk["rules_fetched_at_ns"] > position["margin_rules_at_ns"]
    assert risk["risk_type"] == "CURRENT_RULES_MAINTENANCE_BREACH"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None


def test_risk_worker_refreshes_rules_without_hammering_and_rejects_stale_cache(tmp_path):
    monitor, _, state, _, _, _, _, _, _ = harness(tmp_path)
    worker = SignalRiskWorker(monitor)
    instrument = "BTC_USDT_Perp"
    symbol = "BTC/USDT:PERP"
    assert worker._current_margin_rules(symbol, instrument) is not None
    assert state["rules_calls"] == 1
    state["now"] += 29
    assert worker._current_margin_rules(symbol, instrument) is not None
    assert state["rules_calls"] == 1
    state["rules_offline"] = True
    state["now"] += 2
    assert worker._current_margin_rules(symbol, instrument) is not None
    assert state["rules_calls"] == 2
    state["now"] += 5
    assert worker._current_margin_rules(symbol, instrument) is not None
    assert state["rules_calls"] == 2
    state["now"] += 25
    assert worker._current_margin_rules(symbol, instrument) is None
    assert state["rules_calls"] == 3


def test_risk_worker_stops_auto_paper_when_public_rules_unavailable(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    state["rules_offline"] = True
    rules_calls_before_worker = state["rules_calls"]
    state["now"] += 1
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      Decimal("104"), Decimal("103"), Decimal("105"))

    class Feed:
        def __init__(self, symbols):
            assert symbols == ("BTC/USDT:PERP",)
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def recv(self, *, timeout):
            if not self.sent:
                self.sent = True
                return "BTC/USDT:PERP", quote
            raise TimeoutError

    worker = SignalRiskWorker(monitor, stream_factory=Feed, idle_seconds=0.01)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and monitor.detail(session["id"])["status"] == "ACTIVE":
            time.sleep(0.01)
        assert monitor.detail(session["id"])["status"] == "STOPPED"
    finally:
        worker.stop()
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None
    assert not [row for row in paper.events(account["id"]) if row["kind"] == "CLOSE"]
    risk = [row for row in paper.events(account["id"]) if row["kind"] == "OBSERVE"][-1]["details"]
    assert risk["risk_type"] == "CURRENT_RULES_UNVERIFIABLE"
    assert state["rules_calls"] == rules_calls_before_worker + 1


def test_margin_breach_expires_pending_stop_instead_of_retrying_fill(tmp_path, monkeypatch):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    state["now"] += 1
    stop = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                     Decimal("104"), Decimal("103"), Decimal("105"))

    def unavailable(*_args, **_kwargs):
        raise PaperPerpError("interrupción antes del commit")

    monkeypatch.setattr(monitor.paper, "close_position", unavailable)
    with pytest.raises(SignalMonitorError, match="pendiente"):
        monitor.on_risk_quote(session["id"], stop)
    assert monitor.detail(session["id"])["actions"][-1]["status"] == "PENDING"
    state["now"] += 1
    margin = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                       Decimal("200"), Decimal("199"), Decimal("201"))
    assert monitor.on_risk_quote(session["id"], margin) == "MARGIN_RISK"
    assert monitor.detail(session["id"])["actions"][-1]["status"] == "EXPIRED"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None


def test_auto_paper_stream_gap_never_invents_stop_fill(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    state["now"] += 21
    late = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                     104, 103, 105)
    assert monitor.on_risk_quote(session["id"], late) == "GAP"
    assert monitor.detail(session["id"])["status"] == "GAP"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None
    assert len([row for row in paper.events(account["id"]) if row["kind"] == "CLOSE"]) == 0
    gap_events = [row for row in paper.events(account["id"])
                  if row["details"].get("risk_type") == "STREAM_COVERAGE_GAP"]
    assert len(gap_events) == 1
    assert gap_events[0]["details"]["gap_ns"] == 21_000_000_000
    assert gap_events[0]["details"]["incoming_quote_ns"] == late.event_time_ns
    assert gap_events[0]["details"]["liquidation_status"] == "NOT_DETERMINED"
    valuation = paper.valuation_snapshots(account["id"], now_ns=late.event_time_ns)[position["id"]]
    assert valuation["status"] == "COVERAGE_GAP"
    assert valuation["details"] is None
    assert valuation["last_risk_type"] == "STREAM_COVERAGE_GAP"
    with pytest.raises(PaperPerpError, match="hueco de ticker"):
        paper.close_position(account["id"], position["id"], "gap-same-tick-close",
                             paper.digest({"test": "gap"}), late)
    assert monitor.on_risk_quote(session["id"], late) == "INACTIVE"
    assert len([row for row in paper.events(account["id"])
                if row["details"].get("risk_type") == "STREAM_COVERAGE_GAP"]) == 1
    rules = MarginRules.from_payload(monitor.adapter_factory().get_margin_rules("BTC/USDT:PERP"),
                                     "BTC_USDT_Perp", state["now"] * 1_000_000_000)
    with pytest.raises(PaperPerpError, match="hueco de ticker"):
        paper.observe(account["id"], position["id"], late, rules)

    other = paper.create_account("Segundo", "1000", "500", "800", "5", 2, "0.001", "0")
    state["now"] = START + 5 * STEP + 10
    second = monitor.create(other["id"], strategy["version_id"], dataset["id"],
                            "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(second["id"])
    state["now"] += 21
    assert monitor.expire_risk_gaps() == [second["id"]]
    assert monitor.detail(second["id"])["status"] == "GAP"
    assert paper.account(other["id"])["open_positions"] == 1
    timed_out = [row for row in paper.events(other["id"])
                 if row["details"].get("risk_type") == "STREAM_COVERAGE_GAP"]
    assert len(timed_out) == 1
    assert timed_out[0]["details"]["gap_ns"] == 21_000_000_000
    assert timed_out[0]["details"]["incoming_quote_ns"] is None
    assert monitor.expire_risk_gaps() == []
    assert len([row for row in paper.events(other["id"])
                if row["details"].get("risk_type") == "STREAM_COVERAGE_GAP"]) == 1


def test_restart_candle_poll_cannot_close_after_hour_without_risk_feed(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    state["now"] = START + 7 * STEP + 1
    restarted = SignalMonitor(paper.path, lab.path, adapter_factory=monitor.adapter_factory,
                              clock_ns=monitor.clock_ns)
    result = restarted.poll(session["id"])
    assert result["poll_result"] == "GAP"
    assert "ticker" in result["status_reason"].lower()
    assert len(result["observations"]) == 1  # la segunda vela no se fingió observada
    assert len(result["actions"]) == 1  # no cierre tras el hueco
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None


def test_store_rechecks_auto_close_coverage_after_slow_ticker_fetch(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    state["now"] += 10
    assert monitor.on_risk_quote(session["id"], PerpQuote("BTC_USDT_Perp",
           state["now"] * 1_000_000_000, 100, 99, 101)) == "OBSERVED"
    # La consulta REST comenzó con cobertura válida, pero la respuesta llegó
    # 21 segundos después. El libro debe revalidarla en su propia transacción.
    with paper.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        request_id = "slow-risk-close"
        digest = paper.digest({"session_id": session["id"], "action": "CLOSE",
                               "position_id": position["id"], "request_id": request_id})
        db.execute("""INSERT INTO paper_signal_actions VALUES (?,?,?,?,?,?,?,?,?)""",
                   (session["id"], START + 6 * STEP, "CLOSE", request_id, digest,
                    "PENDING", position["id"], "EXIT_RULE_AT_CANDLE_CLOSE",
                    state["now"] * 1_000_000_000))
    state["now"] += 21
    delayed = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000, 100, 99, 101)
    with pytest.raises(PaperPerpError, match="cobertura continua"):
        paper.close_position(account["id"], position["id"], request_id, digest,
                             delayed, origin_session_id=session["id"])
    older = PerpQuote("BTC_USDT_Perp", (START + 6 * STEP + 5) * 1_000_000_000,
                      100, 99, 101)
    with pytest.raises(PaperPerpError, match="anterior al último ticker"):
        paper.close_position(account["id"], position["id"], request_id, digest,
                             older, origin_session_id=session["id"])
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None


def test_risk_worker_consumes_public_feed_and_exits_without_duplicate_close(tmp_path):
    import time

    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    state["now"] += 1
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000, 104, 103, 105)
    feeds = []

    class Feed:
        def __init__(self, symbols):
            assert symbols == ("BTC/USDT:PERP",)
            self.sent = False
            feeds.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def recv(self, *, timeout):
            assert timeout == 1.0
            if not self.sent:
                self.sent = True
                return "BTC/USDT:PERP", quote
            raise TimeoutError

    worker = SignalRiskWorker(monitor, stream_factory=Feed, idle_seconds=0.01)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and paper.account(account["id"])["open_positions"]:
            time.sleep(0.01)
        assert paper.account(account["id"])["open_positions"] == 0
        assert worker.snapshot()["last_frame_ns"] == quote.event_time_ns
        assert feeds and worker.snapshot()["running"]
    finally:
        worker.stop()
    assert not worker.snapshot()["running"]
    assert len([row for row in paper.events(account["id"]) if row["kind"] == "CLOSE"]) == 1


def test_pending_stream_stop_after_restart_expires_without_retroactive_fill(tmp_path, monkeypatch):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    state["now"] += 1
    stop = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000, 104, 103, 105)
    def unavailable(*_args, **_kwargs):
        raise PaperPerpError("interrupción antes del commit")
    monkeypatch.setattr(monitor.paper, "close_position", unavailable)
    with pytest.raises(SignalMonitorError, match="pendiente"):
        monitor.on_risk_quote(session["id"], stop)
    assert monitor.detail(session["id"])["actions"][-1]["status"] == "PENDING"
    state["now"] += 21
    recovered = SignalMonitor(paper.path, lab.path, adapter_factory=monitor.adapter_factory,
                              clock_ns=monitor.clock_ns)
    assert recovered.poll(session["id"])["poll_result"] == "GAP"
    assert recovered.detail(session["id"])["actions"][-1]["status"] == "EXPIRED"
    assert paper.position(account["id"], position["id"])["closed_at_ns"] is None


def test_auto_paper_rejects_unreachable_short_target(tmp_path):
    monitor, _, _, _, lab, account, dataset, _, _ = harness(tmp_path)
    impossible = definition("SHORT") | {"take_profit": "1"}
    strategy = lab.create_strategy("Objetivo imposible", impossible)
    with pytest.raises(SignalMonitorError, match="objetivo SHORT"):
        monitor.create(account["id"], strategy["version_id"], dataset["id"], "AUTO_PAPER", "1", "2")


def test_auto_paper_concurrent_polls_and_manual_stop_cannot_duplicate_open(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"], "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: monitor.poll(session["id"]), range(2)))
    assert paper.account(account["id"])["open_positions"] == 1
    assert len(monitor.detail(session["id"])["actions"]) == 1
    assert monitor.detail(session["id"])["actions"][0]["status"] == "EXECUTED"
    stopped = monitor.stop(session["id"])
    assert stopped["status"] == "STOPPED" and stopped["position_id"] is not None
    state["now"] = START + 7 * STEP + 1
    assert SignalWorker(monitor).run_once() == {}
    assert paper.account(account["id"])["open_positions"] == 1  # cierre manual aún disponible


def test_auto_paper_pending_close_survives_account_stop_but_pending_open_does_not(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"], "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    advance_with_continuous_risk_feed(monitor, session["id"], state, START + 7 * STEP + 1)
    state["ticker_offline"] = True
    with pytest.raises(SignalMonitorError):
        monitor.poll(session["id"])
    assert monitor.detail(session["id"])["actions"][1]["status"] == "PENDING"
    paper.stop_account(account["id"])
    state["ticker_offline"] = False
    result = monitor.poll(session["id"])
    assert result["status"] == "STOPPED"
    assert result["actions"][1]["status"] == "EXECUTED"
    assert paper.account(account["id"])["open_positions"] == 0

    other = paper.create_account("Pendiente", "1000", "500", "800", "5", 2, "0.001", "0")
    state["now"] = START + 5 * STEP + 10
    pending = monitor.create(other["id"], strategy["version_id"], dataset["id"], "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    state["ticker_offline"] = True
    with pytest.raises(SignalMonitorError):
        monitor.poll(pending["id"])
    paper.stop_account(other["id"])
    state["ticker_offline"] = False
    blocked = monitor.poll(pending["id"])
    assert blocked["actions"][0]["status"] == "REJECTED"
    assert paper.account(other["id"])["open_positions"] == 0


def test_v4_paper_migration_preserves_observer_and_positions(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE paper_signal_candles")
        db.execute("DROP TABLE paper_perp_funding_anchors")
        db.execute("DROP TABLE paper_perp_funding_legacy")
        db.execute("DROP TABLE paper_signal_actions")
        db.execute("DROP INDEX ux_paper_signal_auto_active")
        for column in ("execution_mode", "auto_size", "auto_leverage", "position_id", "risk_last_quote_ns", "source_run_id"):
            db.execute(f"ALTER TABLE paper_signal_sessions DROP COLUMN {column}")
        db.execute("ALTER TABLE paper_perp_positions DROP COLUMN origin_session_id")
        db.execute("PRAGMA user_version=4")
    reopened = PaperPerpStore(paper.path)
    assert reopened.account(account["id"])["id"] == account["id"]
    assert SignalMonitor(paper.path, monitor.lab.path).detail(session["id"])["execution_mode"] == "OBSERVE"
    with sqlite3.connect(paper.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA


def test_v5_paper_migration_adds_risk_heartbeat_without_losing_session(tmp_path):
    monitor, _, _, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE paper_signal_candles")
        db.execute("DROP TABLE paper_perp_funding_anchors")
        db.execute("DROP TABLE paper_perp_funding_legacy")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN risk_last_quote_ns")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN source_run_id")
        db.execute("PRAGMA user_version=5")
    recovered = SignalMonitor(paper.path, lab.path).detail(session["id"])
    assert recovered["id"] == session["id"]
    assert recovered["risk_last_quote_ns"] is None
    assert recovered["execution_mode"] == "OBSERVE"
    with sqlite3.connect(paper.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA


def test_v7_paper_migration_preserves_session_without_source_run(tmp_path):
    monitor, _, _, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE paper_signal_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN source_run_id")
        db.execute("PRAGMA user_version=7")
    reopened = SignalMonitor(paper.path, lab.path)
    assert reopened.detail(session["id"])["source_run_id"] is None
    with sqlite3.connect(paper.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA


def test_causal_signal_is_versioned_persistent_and_never_opens_position(tmp_path):
    monitor, client, state, paper, lab, account, dataset, strategy, rows = harness(tmp_path)
    body = {"account_id": account["id"], "version_id": strategy["version_id"], "dataset_id": dataset["id"]}
    response = client.post("/paper/perp/signals", json=body)
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    assert client.post(f"/paper/perp/signals/{session_id}/poll").json()["poll_result"] == "WAIT"
    assert state["calls"] == 0
    state["now"] = START + 6 * STEP + 1
    observed = client.post(f"/paper/perp/signals/{session_id}/poll")
    assert observed.status_code == 200, observed.text
    result = observed.json()
    assert result["poll_result"] == "OBSERVED"
    assert result["execution"] == "SIGNALS_ONLY_NO_ORDERS"
    event = result["observations"][0]
    bars, _ = validate_candles(rows[:6], "1h", now=state["now"])
    cache = IndicatorCache(bars)
    assert event["entry_match"] == evaluate(definition()["entry"], bars, 5, cache)[0]
    assert event["exit_match"] == evaluate(definition()["exit"], bars, 5, cache)[0]
    assert event["evidence"]["entry"] == evaluate(definition()["entry"], bars, 5, cache)[1]
    assert event["evidence"]["observation_mode"] == "POLL_ON_TIME_REST"
    assert paper.account(account["id"])["open_positions"] == 0
    assert len(client.get(f"/paper/perp/signals?account_id={account['id']}").json()) == 1
    assert SignalMonitor(paper.path, lab.path).detail(session_id)["observations"] == result["observations"]
    assert client.post(f"/paper/perp/signals/{session_id}/poll").json()["poll_result"] == "WAIT"
    assert state["calls"] == 1
    with sqlite3.connect(paper.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA
        assert db.execute("SELECT count(*) FROM paper_signal_observations").fetchone()[0] == 1


def test_grvt_paper_archives_observed_candle_and_backfills_v8(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    observed = monitor.poll(session["id"])
    candle = observed["observations"][0]["evidence"]["candle"]
    with paper.connect() as db:
        row = db.execute("SELECT candle_t,candle_json FROM paper_signal_candles WHERE session_id=?",
                         (session["id"],)).fetchone()
        assert row["candle_t"] == candle["t"]
        assert json.loads(row["candle_json"]) == candle

    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE paper_signal_candles")
        db.execute("PRAGMA user_version=8")
    reopened = SignalMonitor(paper.path, lab.path)
    assert reopened.detail(session["id"])["observations"] == observed["observations"]
    with paper.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA
        row = db.execute("SELECT candle_json FROM paper_signal_candles WHERE session_id=?",
                         (session["id"],)).fetchone()
        assert json.loads(row["candle_json"]) == candle


def test_grvt_paper_full_archive_pages_seed_and_observation(tmp_path):
    monitor, client, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    first = monitor.candle_page(session["id"], limit=2)
    assert [item["origin"] for item in first["candles"]] == ["SEED", "SEED"]
    assert first["has_more"] and first["next_after_t"] == START + STEP
    state["now"] = START + 6 * STEP + 1
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    later = monitor.candle_page(session["id"], first["next_after_t"])
    assert [item["origin"] for item in later["candles"]] == ["SEED"] * 3 + ["OBSERVED"]
    assert not later["has_more"] and later["next_after_t"] is None
    response = client.get(f"/paper/perp/signals/{session['id']}/candles?limit=2")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["candles"] == first["candles"]
    assert client.get(f"/paper/perp/signals/{session['id']}/candles?limit=1001").status_code == 422
    assert client.get(f"/paper/perp/signals/{session['id']}/candles",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403
    assert verify_paper_perp_database(paper.path)["counts"]["paper_signal_all_candles"] == 6


def test_grvt_paper_candle_page_rejects_changed_segment_or_anchor(tmp_path):
    monitor, client, _, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("""UPDATE paper_signal_all_candles
                      SET candle_json=json_set(candle_json,'$.v','999')
                      WHERE session_id=? AND candle_t=?""", (session["id"], START + STEP))
    with pytest.raises(SignalMonitorError, match="Huella de vela PAPER divergente"):
        monitor.candle_page(session["id"], limit=2)
    assert client.get(f"/paper/perp/signals/{session['id']}/candles?limit=2").status_code == 422

    with paper.connect() as db:
        history = json.loads(db.execute("SELECT candles_json FROM paper_signal_sessions WHERE id=?",
                                        (session["id"],)).fetchone()[0])
        db.execute("UPDATE paper_signal_all_candles SET candle_json=? WHERE session_id=? AND candle_t=?",
                   (json.dumps(history[1], sort_keys=True), session["id"], START + STEP))
        db.execute("UPDATE paper_signal_archive_chain SET digest=? WHERE session_id=? AND candle_t=?",
                   ("0" * 64, session["id"], START + STEP))
    with pytest.raises(SignalMonitorError, match="Huella de vela PAPER divergente"):
        monitor.candle_page(session["id"], after_t=START + STEP, limit=1)


def test_grvt_paper_observation_pages_are_bounded_and_local(tmp_path):
    monitor, client, state, _, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    state["now"] = START + 7 * STEP + 1
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    first = monitor.observation_page(session["id"], limit=1)
    assert first["has_more"] and first["observations"][0]["paper_action"] is None
    second = monitor.observation_page(session["id"], first["next_after_t"], 1)
    assert not second["has_more"] and second["observations"][0]["candle_t"] > first["next_after_t"]
    response = client.get(f"/paper/perp/signals/{session['id']}/observations?limit=1")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["observations"] == first["observations"]
    assert client.get(f"/paper/perp/signals/{session['id']}/observations?limit=1001").status_code == 422
    assert client.get(f"/paper/perp/signals/{session['id']}/observations",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_grvt_paper_v10_backfills_full_archive(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("PRAGMA user_version=10")
    reopened = SignalMonitor(paper.path, lab.path)
    assert [item["origin"] for item in reopened.candle_page(session["id"])["candles"]] == [
        "SEED"] * 5 + ["OBSERVED"]
    assert verify_paper_perp_database(paper.path)["schema_version"] == CURRENT_PAPER_SCHEMA


def test_grvt_paper_v11_migrates_archive_chain_with_observation(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_archive_chain")
        db.execute("PRAGMA user_version=11")
    reopened = SignalMonitor(paper.path, lab.path)
    assert reopened.detail(session["id"])["status"] == "ACTIVE"
    report = verify_paper_perp_database(paper.path)
    assert report["schema_version"] == CURRENT_PAPER_SCHEMA
    assert report["counts"]["paper_signal_archive_chain"] == 6


def test_grvt_paper_chain_detects_changed_digest_before_acting(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("""UPDATE paper_signal_archive_chain SET digest=?
                      WHERE session_id=? AND candle_t=?""", ("0" * 64, session["id"], START + STEP))
    with pytest.raises(PaperPerpError, match="cadena de velas divergente"):
        verify_paper_perp_database(paper.path)
    state["now"] = START + 6 * STEP + 1
    result = monitor.poll(session["id"])
    assert result["status"] == "GAP" and result["observations"] == []


def test_grvt_paper_rejects_divergent_full_archive_before_acting(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        db.execute("""UPDATE paper_signal_all_candles
                      SET candle_json=json_set(candle_json,'$.c','999')
                      WHERE session_id=? AND origin='SEED' AND candle_t=?""",
                   (session["id"], START))
    with pytest.raises(PaperPerpError, match="archivo completo divergente"):
        verify_paper_perp_database(paper.path)
    state["now"] = START + 6 * STEP + 1
    result = monitor.poll(session["id"])
    assert result["status"] == "GAP" and result["observations"] == []


def test_grvt_paper_full_archive_failure_rolls_back_observation(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        before = db.execute("SELECT candles_json,indicator_state_json FROM paper_signal_sessions WHERE id=?",
                            (session["id"],)).fetchone()
        db.execute("""CREATE TRIGGER reject_grvt_full_candle BEFORE INSERT ON paper_signal_all_candles
                      WHEN NEW.origin='OBSERVED'
                      BEGIN SELECT RAISE(ABORT,'full GRVT archive unavailable'); END""")
    state["now"] = START + 6 * STEP + 1
    with pytest.raises(sqlcipher3.IntegrityError, match="full GRVT archive unavailable"):
        monitor.poll(session["id"])
    with paper.connect() as db:
        after = db.execute("SELECT candles_json,indicator_state_json FROM paper_signal_sessions WHERE id=?",
                           (session["id"],)).fetchone()
        assert tuple(after) == tuple(before)
        assert db.execute("SELECT count(*) FROM paper_signal_observations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM paper_signal_candles").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM paper_signal_all_candles").fetchone()[0] == 5


def test_grvt_paper_chain_failure_rolls_back_observation_and_state(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        before = db.execute("SELECT candles_json,indicator_state_json FROM paper_signal_sessions WHERE id=?",
                            (session["id"],)).fetchone()
        db.execute(f"""CREATE TRIGGER reject_grvt_chain BEFORE INSERT ON paper_signal_archive_chain
                       WHEN NEW.candle_t > {START + 4 * STEP}
                       BEGIN SELECT RAISE(ABORT,'GRVT chain unavailable'); END""")
    state["now"] = START + 6 * STEP + 1
    with pytest.raises(sqlcipher3.IntegrityError, match="GRVT chain unavailable"):
        monitor.poll(session["id"])
    with paper.connect() as db:
        after = db.execute("SELECT candles_json,indicator_state_json FROM paper_signal_sessions WHERE id=?",
                           (session["id"],)).fetchone()
        assert tuple(after) == tuple(before)
        for table in ("paper_signal_observations", "paper_signal_candles"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        for table in ("paper_signal_all_candles", "paper_signal_archive_chain"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 5


def test_grvt_paper_v10_persists_indicator_state_and_backfills_v9(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    rules = {**definition(), "entry": {"op": "AND", "children": [
        {"kind": "CLOSE_ABOVE_EMA", "period": 2},
        {"kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 2, "slow_period": 3, "signal_period": 2},
        {"kind": "ATR_PCT_GT", "period": 2, "threshold": "0.1"}]}}
    strategy = lab.create_strategy("Indicadores GRVT PAPER", rules)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    observed = monitor.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    with paper.connect() as db:
        row = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session["id"],)).fetchone()
        snapshot = json.loads(row["indicator_state_json"])
        assert snapshot["count"] == 6 and snapshot["last_t"] == row["last_t"]
        assert snapshot["ema"] and snapshot["macd"] and snapshot["atr"]
        assert paper_indicator_state_matches(snapshot, rules, json.loads(row["candles_json"]), 80)
    assert verify_paper_perp_database(paper.path)["schema_version"] == CURRENT_PAPER_SCHEMA

    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("PRAGMA user_version=9")
    reopened = SignalMonitor(paper.path, lab.path)
    assert reopened.detail(session["id"])["status"] == "ACTIVE"
    assert verify_paper_perp_database(paper.path)["schema_version"] == CURRENT_PAPER_SCHEMA


def test_grvt_paper_v9_legacy_indicator_precision_is_preserved(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    strategy = lab.create_strategy("EMA y MACD GRVT legados", {
        **definition(), "entry": {"op": "AND", "children": [
            {"kind": "CLOSE_ABOVE_EMA", "period": 2},
            {"kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 2,
             "slow_period": 3, "signal_period": 2}]}})
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("UPDATE paper_signal_sessions SET engine_version='lab-1.15' WHERE id=?",
                   (session["id"],))
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("PRAGMA user_version=9")
    state["now"] = START + 6 * STEP + 1
    observed = monitor.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    with paper.connect() as db:
        snapshot = json.loads(db.execute("SELECT indicator_state_json FROM paper_signal_sessions WHERE id=?",
                                         (session["id"],)).fetchone()[0])
    assert snapshot["precision"] == 28 and snapshot["ema"] and snapshot["macd"]


def test_grvt_paper_uses_recent_shadow_at_512_candles(tmp_path):
    monitor, _, state, _, lab, account, _, _, rows = harness(tmp_path)
    rows[:] = [{"t": START + index * STEP, "o": str(100 + index % 7),
                "h": str(104 + index % 7), "l": str(96 + index % 7),
                "c": str(101 + index % 7), "v": str(10 + index % 11)}
               for index in range(513)]
    state["now"] = START + 512 * STEP + 10
    bars, gaps = validate_candles(rows[:512], "1h", now=state["now"])
    dataset = lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public", bars, gaps,
                               verified_public=True)
    strategy = lab.create_strategy("Ventana GRVT PAPER", {
        **definition(), "entry": {"op": "AND", "children": [
            {"kind": "CLOSE_ABOVE_EMA", "period": 20},
            {"kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 12,
             "slow_period": 26, "signal_period": 9},
            {"kind": "ATR_PCT_GT", "period": 14, "threshold": "0.1"}]}})
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 513 * STEP + 1
    observed = monitor.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED" and observed["status"] == "ACTIVE"
    assert observed["capacity_remaining"] == 5000 - 513


def test_grvt_paper_rejects_divergent_indicator_snapshot(tmp_path):
    monitor, _, state, paper, lab, account, dataset, _, _ = harness(tmp_path)
    strategy = lab.create_strategy("EMA GRVT PAPER", {
        **definition(), "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}})
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with paper.connect() as db:
        snapshot = json.loads(db.execute("SELECT indicator_state_json FROM paper_signal_sessions WHERE id=?",
                                         (session["id"],)).fetchone()[0])
        snapshot["ema"]["2"]["current"] = "999"
        db.execute("UPDATE paper_signal_sessions SET indicator_state_json=? WHERE id=?",
                   (json.dumps(snapshot), session["id"]))
    with pytest.raises(PaperPerpError, match="divergentes"):
        verify_paper_perp_database(paper.path)
    state["now"] = START + 6 * STEP + 1
    result = monitor.poll(session["id"])
    assert result["status"] == "GAP" and result["observations"] == []


def test_grvt_indicator_snapshot_failure_rolls_back_observation(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    with paper.connect() as db:
        before = db.execute("SELECT indicator_state_json FROM paper_signal_sessions WHERE id=?",
                            (session["id"],)).fetchone()[0]
        db.execute("""CREATE TRIGGER reject_indicator_state BEFORE UPDATE OF indicator_state_json
                      ON paper_signal_sessions
                      BEGIN SELECT RAISE(ABORT,'indicator archive unavailable'); END""")
    state["now"] = START + 6 * STEP + 1
    with pytest.raises(sqlcipher3.IntegrityError, match="indicator archive unavailable"):
        monitor.poll(session["id"])
    with paper.connect() as db:
        assert db.execute("SELECT indicator_state_json FROM paper_signal_sessions WHERE id=?",
                          (session["id"],)).fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM paper_signal_candles").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM paper_signal_observations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM paper_signal_actions").fetchone()[0] == 0


def test_grvt_paper_backup_rejects_altered_observed_candle(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    assert verify_paper_perp_database(paper.path)["counts"]["paper_signal_candles"] == 1
    with paper.connect() as db:
        db.execute("""UPDATE paper_signal_candles
                      SET candle_json=json_set(candle_json,'$.c','999') WHERE session_id=?""",
                   (session["id"],))
    with pytest.raises(PaperPerpError, match="divergentes"):
        verify_paper_perp_database(paper.path)


def test_grvt_paper_stops_before_acting_on_divergent_archive(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    first = monitor.poll(session["id"])
    assert first["poll_result"] == "OBSERVED"
    with paper.connect() as db:
        db.execute("""UPDATE paper_signal_candles
                      SET candle_json=json_set(candle_json,'$.c','999') WHERE session_id=?""",
                   (session["id"],))
    state["now"] = START + 7 * STEP + 1
    result = monitor.poll(session["id"])
    assert result["status"] == "GAP" and result["poll_result"] == "GAP"
    assert result["last_t"] == first["last_t"]
    assert len(result["observations"]) == len(first["observations"])
    assert not any(action["status"] == "PENDING" for action in result["actions"])


def test_grvt_risk_quote_cannot_close_position_with_divergent_archive(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    opened = monitor.poll(session["id"])
    position_id = opened["actions"][0]["position_id"]
    with paper.connect() as db:
        db.execute("""UPDATE paper_signal_candles
                      SET candle_json=json_set(candle_json,'$.c','999') WHERE session_id=?""",
                   (session["id"],))
    state["now"] += 1
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000, 100, 99, 101)
    assert monitor.on_risk_quote(session["id"], quote) == "GAP"
    assert paper.position(account["id"], position_id)["closed_at_ns"] is None
    assert monitor.detail(session["id"])["status"] == "GAP"


def test_grvt_paper_archive_failure_rolls_back_signal_and_auto_open(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    before = monitor.detail(session["id"])
    with paper.connect() as db:
        db.execute("""CREATE TRIGGER reject_signal_candle BEFORE INSERT ON paper_signal_candles
                      BEGIN SELECT RAISE(ABORT,'archive unavailable'); END""")
    state["now"] = START + 6 * STEP + 1
    with pytest.raises(sqlcipher3.IntegrityError, match="archive unavailable"):
        monitor.poll(session["id"])
    after = monitor.detail(session["id"])
    assert after["observations"] == [] and after["actions"] == []
    assert after["last_t"] == before["last_t"]
    assert paper.account(account["id"])["open_positions"] == 0


def test_late_poll_marks_gap_and_never_reconstructs_as_live(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 7 * STEP
    result = monitor.poll(session["id"])
    assert result["poll_result"] == "GAP"
    assert result["observations"] == []
    assert state["calls"] == 0
    assert monitor.poll(session["id"])["poll_result"] == "GAP"
    assert paper.account(account["id"])["open_positions"] == 0


def test_outage_retry_revision_and_stop(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, rows = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    state["offline"] = True
    with pytest.raises(SignalMonitorError, match="red caída"):
        monitor.poll(session["id"])
    assert monitor.detail(session["id"])["status"] == "ACTIVE"
    state["offline"] = False
    state["revised"] = True
    assert monitor.poll(session["id"])["status"] == "GAP"
    assert monitor.detail(session["id"])["observations"] == []
    assert monitor.stop(session["id"])["status"] == "GAP"
    fresh_bars, _ = validate_candles(rows[:6], "1h", now=state["now"])
    fresh_dataset = lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public", fresh_bars, [], verified_public=True)
    new_session = monitor.create(account["id"], strategy["version_id"], fresh_dataset["id"])
    paper.stop_account(account["id"])
    assert monitor.poll(new_session["id"])["status"] == "STOPPED"


@pytest.mark.parametrize("kind", ["missing", "invalid", "old"])
def test_incomplete_grvt_candles_are_retryable_without_paper_action(tmp_path, kind):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    original_factory = monitor.adapter_factory

    class IncompleteSource:
        def get_historical_candles(self, *args, **kwargs):
            rows = original_factory().get_historical_candles(*args, **kwargs)
            if kind == "missing":
                return rows[:2]
            if kind == "invalid":
                return rows[:2] + [{**rows[2], "h": "0"}]
            return [{**row, "t": row["t"] - STEP} for row in rows]

    monitor.adapter_factory = IncompleteSource
    app = FastAPI()
    app.include_router(create_signal_monitor_router(paper.path, lab.path, monitor=monitor))
    response = TestClient(app).post(f"/paper/perp/signals/{session['id']}/poll")
    assert response.status_code == 503, response.text
    assert "GRVT" in response.json()["detail"]
    unchanged = monitor.detail(session["id"])
    assert unchanged["status"] == "ACTIVE" and unchanged["observations"] == []
    assert unchanged["actions"] == [] and unchanged["last_t"] == session["last_t"]
    with pytest.raises(SignalMonitorUnavailable):
        monitor.poll(session["id"])
    monitor.adapter_factory = original_factory
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    assert len(monitor.detail(session["id"])["observations"]) == 1


def test_rejects_wrong_provenance_stale_seed_and_external_origin(tmp_path):
    monitor, client, state, paper, lab, account, dataset, strategy, rows = harness(tmp_path)
    bars, _ = validate_candles(rows[:5], "1h", now=state["now"])
    spot = lab.save_dataset("BTC/USDT", "1h", "binance-spot-public", bars, [])
    with pytest.raises(SignalMonitorError, match="no spot"):
        monitor.create(account["id"], strategy["version_id"], spot["id"])
    spoof = lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public", bars, [])
    assert spoof["verified_public"] is False
    with pytest.raises(SignalMonitorError, match="no spot"):
        monitor.create(account["id"], strategy["version_id"], spoof["id"])
    state["now"] += STEP
    with pytest.raises(SignalMonitorError, match="última vela"):
        monitor.create(account["id"], strategy["version_id"], dataset["id"])
    assert client.get("/paper/perp/signals", headers={"Origin": "https://evil.test"},
                      params={"account_id": account["id"]}).status_code == 403


def test_concurrent_polls_record_one_observation(tmp_path):
    monitor, _, state, _, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: monitor.poll(session["id"]), range(2)))
    assert len(monitor.detail(session["id"])["observations"]) == 1
    assert set(row["poll_result"] for row in results) <= {"OBSERVED", "CHANGED", "WAIT"}


def test_chart_markers_keep_signal_provenance_and_account_isolation(tmp_path):
    monitor, client, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    state["now"] = START + 6 * STEP + 1
    assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    params = {"account_id": account["id"], "symbol": "BTC/USDT:PERP",
              "timeframe": "1h", "since_t": START + 5 * STEP}
    response = client.get("/paper/perp/signals/chart-markers", params=params)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["execution"] == "SIGNALS_SEPARATE_FROM_PAPER_EVENTS"
    assert result["source"] == "GRVT_PAPER_OBSERVED_SIGNALS"
    assert result["truncated"] is False
    assert result["markers"] == [{"session_id": session["id"],
                                  "version_id": strategy["version_id"],
                                  "candle_t": START + 5 * STEP,
                                  "observed_at_ns": state["now"] * 1_000_000_000,
                                  "direction": "SHORT", "execution_mode": "OBSERVE",
                                  "action": None, "action_status": None, "entry_match": True,
                                  "exit_match": True}]
    other = paper.create_account("Otra", "1000", "500", "800", "5", 2, "0.001", "0")
    assert client.get("/paper/perp/signals/chart-markers", params=params | {
        "account_id": other["id"]}).json()["markers"] == []
    assert client.get("/paper/perp/signals/chart-markers", params=params | {
        "timeframe": "4h"}).json()["markers"] == []
    assert client.get("/paper/perp/signals/chart-markers", params=params | {
        "since_t": START + 6 * STEP}).json()["markers"] == []
    assert client.get("/paper/perp/signals/chart-markers", params=params | {
        "symbol": "BTC/USDT"}).status_code == 422


def test_chart_marker_pages_preserve_same_candle_sessions(tmp_path):
    monitor, client, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    sessions = [monitor.create(account["id"], strategy["version_id"], dataset["id"])
                for _ in range(3)]
    state["now"] = START + 6 * STEP + 1
    for session in sessions:
        assert monitor.poll(session["id"])["poll_result"] == "OBSERVED"
    params = {"account_id": account["id"], "symbol": "BTC/USDT:PERP",
              "timeframe": "1h", "since_t": START + 5 * STEP,
              "until_t": START + 5 * STEP, "limit": 1}
    seen = []
    cursor = {}
    for _ in range(3):
        response = client.get("/paper/perp/signals/chart-markers-page", params=params | cursor)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        page = response.json()
        assert page["source"] == "GRVT_PAPER_OBSERVED_SIGNALS"
        assert len(page["markers"]) == 1
        seen.append(page["markers"][0]["session_id"])
        cursor = {"after_t": page["next_after_t"],
                  "after_session_id": page["next_after_session_id"]}
        if not page["has_more"]:
            break
    assert seen == sorted(session["id"] for session in sessions)
    assert len(seen) == 3 and page["has_more"] is False
    assert client.get("/paper/perp/signals/chart-markers-page", params=params | {
        "until_t": START + 4 * STEP}).status_code == 422
    assert client.get("/paper/perp/signals/chart-markers-page", params=params,
                      headers={"Origin": "https://outside.invalid"}).status_code == 403
    other = paper.create_account("Aislada", "1000", "500", "800", "5", 2, "0.001", "0")
    assert client.get("/paper/perp/signals/chart-markers-page", params=params | {
        "account_id": other["id"]}).json()["markers"] == []


def test_worker_retries_then_observes_once_without_opening_positions(tmp_path):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    worker = SignalWorker(monitor)
    assert worker.run_once() == {}
    state["now"] = START + 6 * STEP + 1
    state["offline"] = True
    assert worker.run_once()[session["id"]] == "RETRY"
    assert monitor.detail(session["id"])["observations"] == []
    state["offline"] = False
    assert worker.run_once()[session["id"]] == "OBSERVED"
    event = monitor.detail(session["id"])["observations"][0]
    assert event["evidence"]["observer"] == "LOCAL_WORKER"
    assert worker.run_once() == {}
    assert paper.account(account["id"])["open_positions"] == 0
    restarted = SignalWorker(SignalMonitor(paper.path, lab.path,
                                           adapter_factory=monitor.adapter_factory,
                                           clock_ns=monitor.clock_ns))
    assert restarted.run_once() == {}
    state["now"] = START + 8 * STEP
    assert restarted.run_once()[session["id"]] == "GAP"
    assert len(monitor.detail(session["id"])["observations"]) == 1


def test_worker_runs_in_background_and_stops(tmp_path):
    import time

    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    worker = SignalWorker(monitor, retry_seconds=0.01, idle_seconds=0.1)
    worker.start()
    try:
        session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
        state["now"] = START + 6 * STEP + 1
        worker.wake()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not monitor.detail(session["id"])["observations"]:
            time.sleep(0.01)
        assert len(monitor.detail(session["id"])["observations"]) == 1
        assert worker.snapshot()["running"] is True
        assert paper.account(account["id"])["open_positions"] == 0
    finally:
        worker.stop()
    assert worker.snapshot()["running"] is False


def test_engine_change_blocks_future_signal_without_reinterpretation(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"])
    with sqlite3.connect(paper.path) as db:
        db.execute("UPDATE paper_signal_sessions SET engine_version='lab-older' WHERE id=?", (session["id"],))
    state["now"] = START + 6 * STEP + 1
    result = SignalWorker(monitor).run_once()
    assert result[session["id"]] == "GAP"
    assert monitor.detail(session["id"])["observations"] == []


def test_v2_migrations_preserve_accounts_and_lab_history(tmp_path):
    monitor, _, _, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    _, raw = lab.dataset(dataset["id"])
    bars, _ = validate_candles(raw, "1h", now=START + 5 * STEP + 10)
    legacy = lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public", bars, [])
    with sqlite3.connect(paper.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("DROP TABLE paper_signal_candles")
        db.execute("DROP TABLE paper_perp_funding_anchors")
        db.execute("DROP TABLE paper_perp_funding_legacy")
        db.execute("DROP TABLE paper_signal_actions")
        db.execute("DROP INDEX ux_paper_signal_auto_active")
        db.execute("ALTER TABLE paper_perp_positions DROP COLUMN origin_session_id")
        db.execute("DROP TABLE paper_perp_funding_events")
        db.execute("ALTER TABLE paper_perp_positions DROP COLUMN funding_checked_through_ns")
        db.execute("ALTER TABLE paper_perp_positions DROP COLUMN funding_paid")
        db.execute("DROP TABLE paper_signal_observations")
        db.execute("DROP TABLE paper_signal_sessions")
        db.execute("PRAGMA user_version=2")
    with sqlite3.connect(lab.path) as db:
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
        db.execute("DROP TABLE validation_runs")
        db.execute("DELETE FROM datasets WHERE id=?", (dataset["id"],))
        db.execute("ALTER TABLE datasets DROP COLUMN verified_public")
        db.execute("PRAGMA user_version=2")
    assert paper.account(account["id"])["id"] == account["id"]
    migrated, _ = lab.dataset(legacy["id"])
    assert migrated["verified_public"] is False
    assert lab.version(strategy["version_id"])["id"] == strategy["version_id"]
    with pytest.raises(SignalMonitorError, match="no spot"):
        monitor.create(account["id"], strategy["version_id"], legacy["id"])
