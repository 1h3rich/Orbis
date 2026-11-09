"""Una prueba PAPER aporta reglas y evidencia, nunca capital a un bot REAL."""

from datetime import datetime, timezone
import json

import pytest

from app.lab.engine import CURRENT_ENGINE_VERSION, run_backtest, validate_candles
from app.live_execution.spot_bot_source import SpotBotSourceError, verified_spot_bot_observation
from tests.test_spot_strategy_paper import START, STEP, harness


def bot_source(tmp_path, *, tested=True):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    bars, _ = validate_candles(state["rows"][:3], "1h", now=state["now"])
    run = lab.save_run(dataset["id"], strategy["version_id"],
                       run_backtest(bars, strategy["definition"], "1h"), CURRENT_ENGINE_VERSION)
    session = paper.create(strategy["version_id"], dataset["id"], run["id"] if tested else None)
    state["now"] = START + 4 * STEP + 1
    paper.poll(session["id"])
    return paper, session, state, lab, run


def observation(paper, session, state, *, verify=True):
    return verified_spot_bot_observation(
        paper, session["id"], now=datetime.fromtimestamp(state["now"], timezone.utc),
        verify_backtest=verify)


def test_source_repeats_tested_rules_and_exports_no_fictitious_portfolio(tmp_path):
    paper, session, state, _, run = bot_source(tmp_path)
    source = observation(paper, session, state)
    assert source.entry_match is True and source.candle_t == START + 3 * STEP
    assert source.source_run_id == run["id"] and len(source.observation_digest) == 64
    assert source.observed_at.timestamp() == state["now"]
    assert set(source.binding()) == {"session_id", "version_id", "dataset_id", "dataset_hash", "source_run_id",
                                     "engine_version", "symbol", "timeframe", "definition_hash"}
    assert not any(hasattr(source, name) for name in ("cash", "units", "entry_price", "paper_position", "action"))


def test_source_requires_pinned_backtest_not_just_an_existing_similar_run(tmp_path):
    paper, session, state, _, _ = bot_source(tmp_path, tested=False)
    with pytest.raises(SpotBotSourceError, match="backtest fijado"):
        observation(paper, session, state)


@pytest.mark.parametrize("change", ["state", "observation", "future", "chain", "status", "engine"])
def test_source_rejects_tampered_or_inactive_paper_without_any_order(tmp_path, change):
    paper, session, state, _, _ = bot_source(tmp_path)
    with paper.connect() as db:
        if change == "state":
            raw = json.loads(db.execute("SELECT state_json FROM sessions").fetchone()[0])
            raw["cash"] = "999999"
            db.execute("UPDATE sessions SET state_json=?", (json.dumps(raw),))
        elif change == "observation":
            raw = json.loads(db.execute("SELECT payload_json FROM events").fetchone()[0])
            raw["entry_match"] = False
            db.execute("UPDATE events SET payload_json=?", (json.dumps(raw),))
        elif change == "future":
            db.execute("UPDATE events SET observed_at_ns=?", ((state["now"] + 1) * 1_000_000_000,))
        elif change == "chain":
            db.execute("UPDATE session_archive_chain SET digest=? WHERE candle_t=?", ("0" * 64, START))
        elif change == "status":
            db.execute("UPDATE sessions SET status='STOPPED'")
        else:
            db.execute("UPDATE sessions SET engine_version='lab-1.15'")
    with pytest.raises(SpotBotSourceError):
        observation(paper, session, state)


def test_source_requires_a_paper_observation_not_only_seed_history(tmp_path):
    paper, state, lab, dataset, strategy = harness(tmp_path)
    bars, _ = validate_candles(state["rows"][:3], "1h", now=state["now"])
    run = lab.save_run(dataset["id"], strategy["version_id"],
                       run_backtest(bars, strategy["definition"], "1h"), CURRENT_ENGINE_VERSION)
    session = paper.create(strategy["version_id"], dataset["id"], run["id"])
    with pytest.raises(SpotBotSourceError, match="observación PAPER"):
        observation(paper, session, state)


def test_source_rechecks_result_fingerprint_even_without_repeating_backtest(tmp_path):
    paper, session, state, lab, run = bot_source(tmp_path)
    with lab.connect() as db:
        raw = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (run["id"],)).fetchone()[0])
        raw["metrics"]["net_profit"] = "999999"
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(raw),run["id"]))
    with pytest.raises(SpotBotSourceError, match="backtest de origen"):
        observation(paper, session, state, verify=False)
