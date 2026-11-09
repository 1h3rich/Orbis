"""Sensibilidad del drawdown al orden de operaciones LAB verificadas."""

from decimal import Decimal
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import LabError, run_backtest, validate_candles, validate_strategy
from app.lab.sequence_risk import closed_trade_drawdown, sequence_risk
from app.lab.store import LabStore


def test_sequence_risk_is_reproducible_and_keeps_profit_fixed():
    trades = [{"pnl": str(value)} for value in ([12, -8, 4, -5, 7] * 6)]
    first = sequence_risk(trades, initial_capital="1000", paths=200, seed=173)
    second = sequence_risk(trades, initial_capital="1000", paths=200, seed=173)
    assert first == second
    assert first["method"] == "PERMUTED_CLOSED_TRADE_PNL_V1"
    assert first["trades"] == 30 and first["paths"] == 200
    assert first["net_pnl"] == "60"
    assert (Decimal(first["drawdown_pct"]["p05"])
            <= Decimal(first["drawdown_pct"]["median"])
            <= Decimal(first["drawdown_pct"]["p95"]))
    assert first["observed_closed_trade_drawdown_pct"] == str(
        closed_trade_drawdown([Decimal(row["pnl"]) for row in trades], Decimal("1000"))[0])
    assert first["future_prediction"] is False


def test_sequence_risk_no_loss_has_zero_drawdown():
    result = sequence_risk([{"pnl": "1"}] * 20, initial_capital="100", paths=25, seed=0)
    assert result["drawdown_pct"] == {"p05": "0", "median": "0", "p95": "0", "worst": "0"}
    assert result["ruin_paths"] == 0


@pytest.mark.parametrize("kwargs", (
    {"trades": [{"pnl": "1"}] * 19, "initial_capital": "100"},
    {"trades": [{"pnl": "NaN"}] * 20, "initial_capital": "100"},
    {"trades": [{"pnl": "1"}] * 20, "initial_capital": "0"},
    {"trades": [{"pnl": "1"}] * 20, "initial_capital": "100", "paths": 100001},
))
def test_sequence_risk_rejects_unusable_inputs(kwargs):
    with pytest.raises(LabError):
        sequence_risk(**kwargs)


def test_sequence_risk_api_requires_a_reproducible_saved_run(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    start = 1_735_689_600
    bars, gaps = validate_candles([
        {"t": start + i * 3600, "o": str(105 if i % 2 else 100),
         "h": str(106 if i % 2 else 101), "l": str(104 if i % 2 else 99),
         "c": str(105 if i % 2 else 100), "v": "10"}
        for i in range(160)
    ], "1h", now=start + 162 * 3600)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    version = store.create_strategy("Secuencia", validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.05",
        "take_profit": "0.02", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2},
    }))
    calculated = run_backtest(bars, version["definition"], "1h")
    assert calculated["metrics"]["trades"] >= 20
    saved = store.save_run(dataset["id"], version["version_id"], calculated, ENGINE_VERSION)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    url = f"/lab/runs/{saved['id']}/sequence-risk?paths=40&seed=71"
    first = client.get(url)
    assert first.status_code == 200, first.text
    assert first.json() == client.get(url).json()
    assert first.json()["result_hash"] == saved["result_hash"]
    assert first.json()["trades"] == saved["metrics"]["trades"]
    assert first.json()["future_prediction"] is False
    assert first.headers["cache-control"] == "no-store"
    with store.connect() as db:
        row = db.execute("SELECT trade_json FROM simulated_trades WHERE run_id=? AND index_no=0",
                         (saved["id"],)).fetchone()
        tampered = json.loads(row[0])
        tampered["pnl"] = "9999"
        db.execute("UPDATE simulated_trades SET trade_json=? WHERE run_id=? AND index_no=0",
                   (json.dumps(tampered), saved["id"]))
    denied = client.get(url)
    assert denied.status_code == 409
