"""Riesgo derivado sin alterar versiones de estrategia ni runs persistidos."""

from decimal import Decimal
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.analytics import RISK_VERSION, SECONDS_PER_YEAR, equity_risk
from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import validate_candles, validate_strategy
from app.lab.store import LabStore


START = 1_735_689_600


def test_bar_volatility_uses_sample_std_and_exact_decimals():
    risk = equity_risk([{"equity": "100"}, {"equity": "110"}, {"equity": "99"}])
    assert risk["version"] == RISK_VERSION
    assert risk["returns_count"] == 2
    assert Decimal(risk["bar_volatility_pct"]).quantize(Decimal("0.001")) == Decimal("14.142")
    assert equity_risk([{"equity": "100"}, {"equity": "100"}, {"equity": "100"}])["bar_volatility_pct"] == "0"
    assert equity_risk([{"equity": "100"}, {"equity": "110"}])["bar_volatility_pct"] is None
    assert equity_risk([{"equity": "100"}, {"equity": "0"}, {"equity": "10"}])["bar_volatility_pct"] is None


def test_annualized_return_requires_full_year_and_positive_causal_curve():
    one_year = equity_risk([{"t": 0, "equity": "100"},
                            {"t": SECONDS_PER_YEAR, "equity": "110"}])
    assert Decimal(one_year["annualized_return_pct"]) == Decimal("10")
    assert one_year["bar_volatility_pct"] is None
    two_years = equity_risk([{"t": 0, "equity": "100"},
                             {"t": SECONDS_PER_YEAR, "equity": "100"},
                             {"t": 2 * SECONDS_PER_YEAR, "equity": "121"}])
    assert Decimal(two_years["annualized_return_pct"]).quantize(Decimal("0.000001")) == Decimal("10.000000")
    short = equity_risk([{"t": 0, "equity": "100"},
                         {"t": SECONDS_PER_YEAR - 1, "equity": "200"}])
    assert short["annualized_return_pct"] is None
    assert "inferior a un año" in short["annualized_reason"]
    assert equity_risk([{"t": 0, "equity": "100"},
                        {"t": SECONDS_PER_YEAR, "equity": "0"}])["annualized_return_pct"] is None
    assert equity_risk([{"t": SECONDS_PER_YEAR, "equity": "100"},
                        {"t": 0, "equity": "110"}])["annualized_return_pct"] is None


def test_old_engine_runs_gain_read_only_risk_without_migrating(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, gaps = validate_candles([
        {"t": START + i * 3600, "o": str(100 + i), "h": str(102 + i),
         "l": str(99 + i), "c": str(101 + i), "v": "10"}
        for i in range(100)
    ], "1h", now=START + 102 * 3600)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "0.2",
        "fee_rate": "0.001", "slippage_rate": "0.001", "stop_loss": "0.03",
        "take_profit": "0.08", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 10},
    }))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    request = {"dataset_id": dataset["id"], "version_id": strategy["version_id"]}
    run_response = client.post("/lab/runs", json=request)
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()
    assert run["engine_version"] == ENGINE_VERSION
    assert run["risk"]["version"] == RISK_VERSION
    assert run["risk"]["bar_volatility_pct"] is not None
    assert run["risk"]["annualized_return_pct"] is None
    validation_response = client.post("/lab/validations", json=request)
    assert validation_response.status_code == 200, validation_response.text
    validation = validation_response.json()
    assert validation["risk"]["train"]["version"] == RISK_VERSION
    assert validation["risk"]["out_of_sample"]["returns_count"] == 29
    with sqlite3.connect(path) as db:
        assert '"risk"' not in db.execute("SELECT result_json FROM runs WHERE id=?", (run["id"],)).fetchone()[0]
        assert '"risk"' not in db.execute("SELECT result_json FROM validation_runs WHERE id=?",
                                          (validation["id"],)).fetchone()[0]
    assert client.get("/lab/runs/" + run["id"]).json() == run
    assert client.get("/lab/validations/" + validation["id"]).json() == validation
