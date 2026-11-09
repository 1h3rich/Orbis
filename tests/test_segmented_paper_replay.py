"""Paridad de señales al reproducir observaciones con ventana acotada."""

from decimal import Decimal
import json
import sqlite3

import pytest

from app.core.paper_archive_chain import next_archive_digest
from app.lab.engine import (CURRENT_ENGINE_VERSION, Candle, IndicatorCache,
                            evaluate, validate_strategy)
from app.lab.paper_signal_replay import (verify_archived_paper_signals,
                                         verify_segmented_base_signals)
from app.lab.store import LabStore


START = 1_735_689_600
STEP = 3600


def history(count: int) -> list[Candle]:
    bars = []
    for index in range(count):
        close = Decimal(100) + Decimal((index * 17) % 43) / 10 + Decimal(index) / 1000
        opened = close + (Decimal(1) if index % 4 else Decimal(-1))
        bars.append(Candle(START + index * STEP, opened,
                           max(opened, close) + Decimal(2 + index % 3),
                           min(opened, close) - Decimal(2 + index % 5), close,
                           Decimal(10 + (index * 7) % 19)))
    return bars


def definition() -> dict:
    return validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
        "fee_rate": "0.001", "slippage_rate": "0", "stop_loss": "0.03",
        "take_profit": "0.08",
        "entry": {"op": "AND", "children": [
            {"op": "OR", "children": [
                {"kind": "RSI_LT", "period": 14, "threshold": "40"},
                {"kind": "EMA_CROSS_ABOVE_EMA", "period": 20, "slow_period": 50}]},
            {"kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 12,
             "slow_period": 26, "signal_period": 9}]},
        "exit": {"op": "OR", "children": [
            {"kind": "ATR_PCT_GT", "period": 14, "threshold": "2"},
            {"kind": "CLOSE_BELOW_SMA", "period": 200},
            {"kind": "STRUCTURE_LH_LL", "period": 90, "pivot_bars": 3}]},
    })


def observations(bars: list[Candle], seed_count: int, rules: dict):
    cache = IndicatorCache(bars)
    for index in range(seed_count, len(bars)):
        entry, entry_evidence = evaluate(rules["entry"], bars, index, cache,
                                         base_timeframe="1h")
        exit_match, exit_evidence = evaluate(rules["exit"], bars, index, cache,
                                             base_timeframe="1h")
        yield {"candle_t": bars[index].t, "candle": bars[index].json(),
               "entry_match": entry, "exit_match": exit_match,
               "entry": entry_evidence, "exit": exit_evidence}


def test_segmented_replay_matches_nested_rules_across_ten_thousand_bars():
    bars, rules, seed_count = history(10050), definition(), 5000
    report = verify_segmented_base_signals(
        rules, [bar.json() for bar in bars[:seed_count]],
        observations(bars, seed_count, rules), timeframe="1h",
        engine_version=CURRENT_ENGINE_VERSION)
    assert report["verified"] and report["checked_observations"] == 5050


def test_segmented_replay_crosses_warmup_boundary_and_detects_changed_evidence():
    bars, rules, seed_count = history(520), definition(), 3
    seed = [bar.json() for bar in bars[:seed_count]]
    correct = verify_segmented_base_signals(
        rules, seed, observations(bars, seed_count, rules), timeframe="1h",
        engine_version=CURRENT_ENGINE_VERSION)
    assert correct["verified"] and correct["checked_observations"] == 517

    def altered():
        for index, item in enumerate(observations(bars, seed_count, rules)):
            if index == 509:
                item["entry_match"] = not item["entry_match"]
            yield item
    changed = verify_segmented_base_signals(
        rules, seed, altered(), timeframe="1h", engine_version=CURRENT_ENGINE_VERSION)
    assert changed["status"] == "mismatch" and changed["checked_observations"] == 509
    assert changed["first_mismatch_t"] == bars[seed_count + 509].t


@pytest.mark.parametrize("kind,count,seed_count", [("spot", 10050, 5000),
                                                    ("grvt", 520, 300)])
def test_archived_replay_streams_source_and_observations_without_session_history(
        tmp_path, kind, count, seed_count):
    bars, rules = history(count), definition()
    symbol = "BTC/USDT" if kind == "spot" else "BTC/USDT:PERP"
    source = "mexc-spot-public" if kind == "spot" else "grvt-perp-trade-public"
    lab = LabStore(tmp_path / "lab.db")
    seed = bars[:seed_count]
    dataset = lab.save_dataset(symbol, "1h", source, seed, [], verified_public=True)
    strategy = lab.create_strategy("Replay archivado", rules)
    session_id = "session-archive-proof"
    session = {"id": session_id, "version_id": strategy["version_id"],
               "dataset_id": dataset["id"], "dataset_hash": dataset["hash"],
               "symbol": symbol, "timeframe": "1h", "last_t": bars[-1].t,
               "engine_version": CURRENT_ENGINE_VERSION}
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    archive = "session_candles" if kind == "spot" else "paper_signal_all_candles"
    chain = "session_archive_chain" if kind == "spot" else "paper_signal_archive_chain"
    db.execute(f"CREATE TABLE {archive} (session_id TEXT,candle_t INTEGER,candle_json TEXT,origin TEXT)")
    db.execute(f"CREATE TABLE {chain} (session_id TEXT,candle_t INTEGER,digest TEXT)")
    if kind == "spot":
        db.execute("CREATE TABLE events (session_id TEXT,candle_t INTEGER,kind TEXT,payload_json TEXT)")
    else:
        db.execute("""CREATE TABLE paper_signal_observations
                      (session_id TEXT,candle_t INTEGER,entry_match INTEGER,
                       exit_match INTEGER,evidence_json TEXT)""")
    previous = None
    for bar in bars:
        origin = "SEED" if bar.t <= seed[-1].t else "OBSERVED"
        raw = json.dumps(bar.json(), sort_keys=True, separators=(",", ":"))
        db.execute(f"INSERT INTO {archive} VALUES (?,?,?,?)", (session_id, bar.t, raw, origin))
        previous = next_archive_digest(previous, session_id, bar.t, origin, raw, kind)
        db.execute(f"INSERT INTO {chain} VALUES (?,?,?)", (session_id, bar.t, previous))
    for item in observations(bars, seed_count, rules):
        payload = {key: item[key] for key in ("candle", "entry_match", "exit_match", "entry", "exit")}
        if kind == "spot":
            db.execute("INSERT INTO events VALUES (?,?,?,?)",
                       (session_id, item["candle_t"], "OBSERVATION", json.dumps(payload)))
        else:
            db.execute("INSERT INTO paper_signal_observations VALUES (?,?,?,?,?)",
                       (session_id, item["candle_t"], int(item["entry_match"]),
                        None if item["exit_match"] is None else int(item["exit_match"]),
                        json.dumps(payload)))
    # La sesión no contiene history_json/candles_json; el replay depende del archivo.
    report = verify_archived_paper_signals(lab, db, session, rules, kind, page_size=512)
    assert report["verified"] and report["checked_observations"] == count - seed_count
    db.execute(f"UPDATE {chain} SET digest=? WHERE candle_t=?", ("0" * 64, seed[-1].t))
    changed = verify_archived_paper_signals(lab, db, session, rules, kind, page_size=512)
    assert changed["status"] == "mismatch" and not changed["verified"]
    db.close()
