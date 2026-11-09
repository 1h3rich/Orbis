"""Cálculos, límites, persistencia y frontera local del DCA histórico."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.api import create_automation_router
from app.automation.dca import (
    AVERAGE_ENGINE_VERSION, DcaState, ENGINE_VERSION, SMA_ENGINE_VERSION, VOLUME_ENGINE_VERSION,
    apply_dca_step, dca_engine_for_rule, dca_trigger_candidates, simulate_dca as _simulate_dca,
    validate_dca,
)
from app.automation.store import AutomationStore
from app.automation.verification import verify_dca_run
from app.lab.engine import Candle, LabError, validate_candles
from app.lab.secure_backup import create_secure_lab_backup
from app.lab.store import LabStore, content_hash


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
STEP = 86400


def history(prices=(100, 90, 80, 70)):
    rows = [{"t": START + i * STEP, "o": str(p), "h": str(p + 1),
             "l": str(p - 1), "c": str(p), "v": "10"} for i, p in enumerate(prices)]
    return validate_candles(rows, "1d", now=START + 10 * STEP)[0]


def config(**changes):
    raw = {"frequency": "daily", "amount": "100", "budget_total": "1000",
           "monthly_limit": "1000", "max_exposure": "1", "fee_rate": "0",
           "slippage_rate": "0", "max_extra_activations": 2, "cooldown_bars": 0,
           "dips": [{"drop_pct": "0.1", "amount": "50"}, {"drop_pct": "0.2", "amount": "100"}]}
    raw.update(changes)
    return validate_dca(raw)


def simulate_dca(candles, rule, *, timeframe="1d", engine_version=ENGINE_VERSION):
    return _simulate_dca(candles, rule, timeframe=timeframe, engine_version=engine_version)


def test_dca_relative_volume_uses_only_closed_bars_and_preserves_reference_variants():
    bars = [replace(bar, v=Decimal(volume)) for bar, volume in
            zip(history((100, 100, 90, 80, 70, 130)), (10, 10, 20, 9, 5, 10000), strict=True)]
    baseline = config(frequency="monthly")
    rule = config(frequency="monthly", volume_gate={"period": 2, "min_ratio": "2"})
    assert dca_engine_for_rule(rule) == VOLUME_ENGINE_VERSION
    actual = _simulate_dca(bars, rule, timeframe="1d")["results"]
    branch = actual["conditional"]
    assert [(row["kind"], row["volume_ratio"], row["signal_time"], row["execution_time"], row["price"])
            for row in branch["events"]] == [
        ("BASE", "2", bars[2].t, bars[3].t, "80"), ("DIP", "2", bars[2].t, bars[3].t, "80")]
    assert Decimal(branch["metrics"]["units"]) == Decimal("1.875")
    assert Decimal(branch["metrics"]["net_profit"]) == Decimal("93.75")
    assert [(row["kind"], row["reason"], row["volume_ratio"]) for row in branch["skipped"][:3]] == [
        ("BASE", "VOLUME_WARMUP", None), ("BASE", "VOLUME_WARMUP", None), ("DIP", "VOLUME_BELOW_MINIMUM", "0.6")]
    previous = simulate_dca(bars, baseline)["results"]
    assert actual["fixed"] == previous["fixed"] and actual["lump"] == previous["lump"]
    bars[-1] = replace(bars[-1], v=Decimal(0))
    assert _simulate_dca(bars, rule, timeframe="1d")["results"] == actual
    with pytest.raises(LabError, match="motor DCA fijado"):
        simulate_dca(bars, rule)
    with pytest.raises(LabError, match="no admite el filtro DCA"):
        dca_trigger_candidates(rule, signal_close=Decimal("90"), reference=Decimal("100"),
            period="2025-01", last_period=None, triggered=set(), extra_count=0, index=3,
            last_extra_index=None, allow_average=True)


def test_dca_relative_volume_zero_baseline_does_not_spend():
    bars = [replace(bar, v=Decimal(volume)) for bar, volume in
            zip(history((100, 100, 90, 80)), (0, 0, 100, 100), strict=True)]
    rule = config(volume_gate={"period": 2, "min_ratio": "2"})
    result = _simulate_dca(bars, rule, timeframe="1d")["results"]
    assert not result["conditional"]["events"] and result["fixed"]["events"]
    assert {row["reason"] for row in result["conditional"]["skipped"]} == {
        "VOLUME_WARMUP", "VOLUME_BASELINE_ZERO"}
    assert all(row["volume_ratio"] is None for row in result["conditional"]["skipped"])


@pytest.mark.parametrize("gate", [None, {}, {"period": True, "min_ratio": "2"},
    {"period": 1, "min_ratio": "2"}, {"period": 121, "min_ratio": "2"}, {"period": 2, "min_ratio": "0"},
    {"period": 2, "min_ratio": "101"}, {"period": 2, "min_ratio": "NaN"}, {"period": 2, "min_ratio": "2", "unknown": 0}])
def test_dca_rejects_invalid_volume_gate(gate):
    with pytest.raises(LabError):
        config(volume_gate=gate)


def test_dca_disallows_mixing_volume_with_an_existing_condition():
    with pytest.raises(LabError, match="sin otro filtro"):
        config(volume_gate={"period": 2, "min_ratio": "2"}, sma_gate={"period": 2, "condition": "below"})


def test_sma_gate_delays_weekly_buy_filters_dips_and_uses_only_known_closes():
    candles = history((100, 110, 90, 200, 200))
    legacy = config(frequency="weekly")
    gated = legacy | {"sma_gate": {"period": 2, "condition": "below"}}
    result = _simulate_dca(candles, validate_dca(gated), timeframe="1d")
    assert dca_engine_for_rule(gated) == SMA_ENGINE_VERSION
    conditioned = result["results"]["conditional"]
    assert [(item["reason"], item["sma_value"]) for item in conditioned["skipped"]] == [
        ("SMA_WARMUP", None), ("SMA_CONDITION_NOT_MET", "105")]
    assert [(item["kind"], item["signal_time"], item["execution_time"], item["price"], item["sma_value"])
            for item in conditioned["events"]] == [
        ("BASE", candles[2].t, candles[3].t, "200", "100"),
        ("DIP", candles[2].t, candles[3].t, "200", "100")]
    baseline = simulate_dca(candles, legacy)["results"]
    assert result["results"]["fixed"] == baseline["fixed"]
    assert result["results"]["lump"] == baseline["lump"]
    with pytest.raises(LabError, match="no corresponde"):
        simulate_dca(candles, gated, engine_version=ENGINE_VERSION)
    with pytest.raises(LabError, match="solo está disponible"):
        dca_trigger_candidates(gated, signal_close=Decimal("90"), reference=Decimal("100"),
            period="2025-W01", last_period=None, triggered=set(), extra_count=0, index=3, last_extra_index=None)


@pytest.mark.parametrize("condition,enabled", [("above", True), ("below", False)])
def test_sma_gate_strict_inequality_and_failed_dip(condition, enabled):
    candles = history((100, 70, 80, 80, 100))
    result = _simulate_dca(candles, config(sma_gate={"period": 2, "condition": condition}), timeframe="1d")
    branch = result["results"]["conditional"]
    assert any(item["kind"] == "DIP" and item["reason"] == "SMA_CONDITION_NOT_MET"
               for item in branch["skipped"]) is enabled
    assert any(item["signal_time"] == candles[2].t for item in branch["events"]) is enabled
    assert not any(item["signal_time"] == candles[3].t for item in branch["events"])
    assert any(item["time"] == candles[4].t and item["sma_value"] == "80" for item in branch["skipped"])


@pytest.mark.parametrize("gate", [None, {}, {"period": True, "condition": "above"},
    {"period": 1, "condition": "below"}, {"period": 121, "condition": "below"},
    {"period": 20, "condition": "equal"}, {"period": 20, "condition": "below", "other": 3}])
def test_dca_rejects_invalid_sma_gate(gate):
    with pytest.raises(LabError, match="Filtro SMA inválido"):
        config(sma_gate=gate)


def test_moving_average_changes_requested_amount_without_using_execution_close():
    candles = history((100, 100, 160, 125, 90, 140, 150))
    adjustment = {"kind": "ema", "period": 3, "condition": "above",
                  "matched_factor": "1.5", "unmatched_factor": "0.5"}
    rule = config(ma_adjustment=adjustment)
    assert dca_engine_for_rule(rule) == AVERAGE_ENGINE_VERSION
    ema = _simulate_dca(candles, rule, timeframe="1d")["results"]
    sma = _simulate_dca(candles, config(ma_adjustment=adjustment | {"kind": "sma"}), timeframe="1d")["results"]
    legacy = simulate_dca(candles, config())["results"]
    assert ema["fixed"] == sma["fixed"] == legacy["fixed"]
    assert ema["lump"] == sma["lump"] == legacy["lump"]
    assert [event["reason"] for event in ema["conditional"]["skipped"]] == ["MA_WARMUP"] * 2
    ema_base = next(event for event in ema["conditional"]["events"] if event["signal_time"] == candles[3].t)
    sma_base = next(event for event in sma["conditional"]["events"] if event["signal_time"] == candles[3].t)
    assert Decimal(ema_base["ma_value"]) == Decimal("122.5")
    assert Decimal(sma_base["ma_value"]) > Decimal("125")
    assert ema_base["base_requested"] == sma_base["base_requested"] == "100"
    assert ema_base["price"] == sma_base["price"] == "90"
    assert (Decimal(ema_base["spent"]), Decimal(sma_base["spent"])) == (Decimal("150"), Decimal("50"))
    dip = next(event for event in ema["conditional"]["events"] if event["kind"] == "DIP")
    assert dip["amount_factor"] == "0.5" and Decimal(dip["spent"]) == Decimal("25")


def test_average_equality_uses_alternate_amount_and_zero_defers_monthly_buy():
    candles = history((100, 100, 100, 80, 120, 130))
    adjustment = {"kind": "ema", "period": 3, "condition": "above",
                  "matched_factor": "2", "unmatched_factor": "0"}
    result = _simulate_dca(candles, config(frequency="monthly", ma_adjustment=adjustment), timeframe="1d")
    conditional = result["results"]["conditional"]
    equal = next(event for event in conditional["skipped"] if event["time"] == candles[3].t)
    assert equal["ma_value"] == "100" and equal["ma_matched"] is False
    assert equal["reason"] == "MA_ZERO_AMOUNT" and Decimal(equal["requested"]) == 0
    assert [(event["kind"], event["execution_time"], Decimal(event["spent"])) for event in conditional["events"]] == [
        ("BASE", candles[5].t, Decimal("200"))]


@pytest.mark.parametrize("changes", [{"period": 1}, {"period": 121}, {"period": True}, {"kind": "wma"},
    {"condition": "equals"}, {"matched_factor": "11"}, {"matched_factor": "0.5"}, {"unmatched_factor": "-0.01"},
    {"unmatched_factor": 1}, {"unexpected": True}])
def test_dca_rejects_invalid_moving_average_adjustments(changes):
    adjustment = {"kind": "ema", "period": 3, "condition": "below", "matched_factor": "2", "unmatched_factor": "0.5"}
    with pytest.raises(LabError):
        config(ma_adjustment=adjustment | changes)
    with pytest.raises(LabError):
        config(ma_adjustment=adjustment, sma_gate={"period": 3, "condition": "below"})


def test_dca_direct_engine_rejects_gaps_and_invalid_candles():
    bars = history()
    rule = config()
    assert simulate_dca(bars, rule, timeframe="1d") == simulate_dca(bars, rule)
    with pytest.raises(TypeError, match="timeframe"):
        _simulate_dca(bars, rule)
    with pytest.raises(LabError, match="Temporalidad"):
        simulate_dca(bars, rule, timeframe="2d")
    for damaged in (
        [bars[0], bars[2], bars[3]],
        [bars[0], bars[1], bars[1]],
        [bars[1], bars[0], bars[2]],
        [bars[0], bars[1], Candle(bars[2].t, bars[2].o, bars[2].h,
                                    bars[2].l, Decimal("NaN"), bars[2].v)],
    ):
        with pytest.raises(LabError, match="Histórico DCA"):
            simulate_dca(damaged, rule, timeframe="1d")
    # La primera distancia puede parecer otra temporalidad válida; la API
    # conoce la temporalidad del dataset y no debe inferirla del hueco.
    every_other_day = [bars[0], bars[2], history((80, 70, 60, 50, 40))[4]]
    with pytest.raises(LabError, match="Histórico DCA"):
        simulate_dca(every_other_day, rule, timeframe="1d")


def test_conditioned_fixed_and_lump_use_previous_close_and_same_history():
    bars = history()
    result = simulate_dca(bars, config())
    conditional = result["results"]["conditional"]
    assert [(row["kind"], row["signal_time"], row["execution_time"]) for row in conditional["events"]] == [
        ("BASE", START, START + STEP),
        ("BASE", START + STEP, START + 2 * STEP),
        ("DIP", START + STEP, START + 2 * STEP),
        ("BASE", START + 2 * STEP, START + 3 * STEP),
        ("DIP", START + 2 * STEP, START + 3 * STEP),
    ]
    assert conditional["metrics"]["invested"] == "450"
    assert result["results"]["fixed"]["metrics"]["invested"] == "300"
    assert result["results"]["lump"]["metrics"]["invested"] == "1000"
    assert Decimal(conditional["metrics"]["remaining_cash"]) + Decimal(conditional["metrics"]["invested"]) == 1000
    with localcontext() as context:
        context.prec = 300
        assert Decimal(conditional["metrics"]["net_profit"]) == Decimal(conditional["metrics"]["final_equity"]) - 1000
    assert simulate_dca(bars, config()) == result


def test_monthly_exposure_costs_and_activation_limits_are_enforced():
    limited = simulate_dca(history(), config(monthly_limit="150"))["results"]["conditional"]
    assert limited["metrics"]["invested"] == "150"
    assert limited["metrics"]["remaining_cash"] == "850"
    assert limited["events"][1]["requested"] == "100" and limited["events"][1]["spent"] == "50"

    exposure = simulate_dca(history(), config(max_exposure="0.1"))["results"]["conditional"]
    assert Decimal(exposure["metrics"]["invested"]) < 450
    assert all(Decimal(row["spent"]) <= Decimal(row["requested"]) for row in exposure["events"])

    costly = simulate_dca(history(), config(fee_rate="0.01", slippage_rate="0.02"))["results"]["conditional"]
    first = costly["events"][0]
    assert Decimal(first["price"]) == Decimal("90") * Decimal("1.02")
    assert Decimal(first["spent"]) == Decimal(first["units"]) * Decimal(first["price"]) + Decimal(first["fee"])
    assert Decimal(costly["metrics"]["fees"]) > 0

    simultaneous = simulate_dca(history((100, 70, 70, 70)), config(max_extra_activations=1))["results"]["conditional"]
    assert simultaneous["metrics"]["conditional_activations"] == 1
    assert len([row for row in simultaneous["events"] if row["kind"] == "DIP"]) == 1
    assert any(row["reason"] == "ACTIVATION_OR_COOLDOWN_LIMIT" for row in simultaneous["skipped"])


def test_lump_sum_comparison_uses_available_budget_not_dca_monthly_cap():
    bars = history()
    rule = config(monthly_limit="150")
    current = simulate_dca(bars, rule)
    assert current["results"]["conditional"]["metrics"]["invested"] == "150"
    assert current["results"]["fixed"]["metrics"]["invested"] == "150"
    assert current["results"]["lump"]["metrics"]["invested"] == "1000"
    assert current["results"]["lump"]["metrics"]["buys"] == 1
    assert "límite mensual solo" in current["assumptions"][2]

    legacy = simulate_dca(bars, rule, engine_version="dca-1.2")
    assert legacy["results"]["lump"]["metrics"]["invested"] == "150"
    assert legacy["assumptions"][2] == (
        "Las tres variantes comparten presupuesto, límite mensual, exposición, comisión y slippage")
    exposure = simulate_dca(bars, config(monthly_limit="150", max_exposure="0.1"))
    assert Decimal(exposure["results"]["lump"]["metrics"]["invested"]) < 1000


def test_dca_capital_return_uses_same_initial_budget_and_keeps_idle_cash():
    bars = history()
    result = simulate_dca(bars, config(monthly_limit="150"))
    with localcontext() as context:
        context.prec = 300
        for mode in ("conditional", "fixed", "lump"):
            metrics = result["results"][mode]["metrics"]
            initial = Decimal(result["rule"]["budget_total"])
            final = Decimal(metrics["final_equity"])
            assert Decimal(metrics["return_on_initial_capital_pct"]) == (final - initial) / initial * 100
            assert Decimal(metrics["remaining_cash"]) + Decimal(metrics["invested"]) == initial
            assert Decimal(metrics["net_profit"]) == final - initial
    fixed = result["results"]["fixed"]["metrics"]
    assert fixed["return_on_initial_capital_pct"] != fixed["return_on_invested_pct"]
    assert "return_on_initial_capital_pct" not in simulate_dca(
        bars, config(monthly_limit="150"), engine_version="dca-1.3")["results"]["fixed"]["metrics"]


def test_equity_volatility_compares_variants_on_the_same_calendar_without_rewriting_legacy():
    bars = history((100, 100, 110))
    rule = config(dips=[])
    current = simulate_dca(bars, rule)
    fixed = current["results"]["fixed"]["metrics"]
    lump = current["results"]["lump"]["metrics"]
    assert [Decimal(row["value"]) for row in current["results"]["fixed"]["equity"]] == [
        Decimal("1000"), Decimal("1000"), Decimal("1010")]
    assert Decimal(fixed["equity_volatility_per_bar_pct"]) == Decimal("0.5")
    assert Decimal(lump["equity_volatility_per_bar_pct"]) == Decimal("5")
    assert fixed["equity_return_observations"] == lump["equity_return_observations"] == 2
    assert "no se anualiza" in current["assumptions"][-1]

    legacy = simulate_dca(bars, rule, engine_version="dca-1.5")
    assert "equity_volatility_per_bar_pct" not in legacy["results"]["fixed"]["metrics"]
    assert "equity_return_observations" not in legacy["results"]["fixed"]["metrics"]
    assert len(legacy["assumptions"]) == 5
    for mode in ("conditional", "fixed", "lump"):
        prior = current["results"][mode]["metrics"].copy()
        prior.pop("equity_volatility_per_bar_pct")
        prior.pop("equity_return_observations")
        assert legacy["results"][mode]["metrics"] == prior


def test_saved_dca_15_run_still_verifies_after_new_engine(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    bars = history((100, 100, 110))
    lab = LabStore(lab_path)
    dataset = lab.save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    store = AutomationStore(automation_path)
    rule = store.create_rule("Plan anterior", config(dips=[]))
    saved = store.save_run(
        rule["version_id"], dataset["id"], dataset["hash"],
        simulate_dca(bars, rule["definition"], engine_version="dca-1.5"),
        "dca-1.5", lab_path=lab_path,
        expected_definition_hash=content_hash(rule["definition"]),
    )
    assert verify_dca_run(store, lab, saved)["status"] == "verified"
    assert "equity_volatility_per_bar_pct" not in store.run(saved["id"])["results"]["fixed"]["metrics"]


def test_dca_extreme_decimals_keep_cash_and_units_exact():
    rule = config(amount="1e-40", budget_total="9e40", monthly_limit="9e40",
                  fee_rate="0", slippage_rate="0", dips=[])
    bars = [Candle(START + i * STEP, Decimal("1"), Decimal("2"),
                   Decimal("0.5"), Decimal("1"), Decimal("1")) for i in range(3)]
    metrics = simulate_dca(bars, rule)["results"]["fixed"]["metrics"]
    with localcontext() as context:
        context.prec = 300
        assert Decimal(metrics["invested"]) == Decimal("2e-40")
        assert Decimal(metrics["remaining_cash"]) == Decimal("9e40") - Decimal("2e-40")
        assert Decimal(metrics["remaining_cash"]) + Decimal(metrics["invested"]) == Decimal("9e40")
        assert Decimal(metrics["final_equity"]) == Decimal("9e40")
        old = simulate_dca(bars, rule, engine_version="dca-1.5")["results"]["fixed"]["metrics"]
        assert Decimal(old["remaining_cash"]) == Decimal("9e40") - Decimal("2e-40")

    # La función pública no depende de la precisión decimal del llamador.
    state = DcaState(cash=Decimal("9e40"), reference=Decimal("1"))
    apply_dca_step(state, rule, signal_time=bars[0].t, signal_close=Decimal("1"),
                   execution_time=bars[1].t, execution_price=Decimal("1"),
                   mark_price=Decimal("1"), index=1, mode="fixed")
    with localcontext() as context:
        context.prec = 300
        assert state.cash + state.spent == Decimal("9e40")
        assert state.cash == Decimal("9e40") - Decimal("1e-40")


def test_high_watermark_dip_uses_only_prior_closed_prices():
    bars = history((100, 150, 120, 120))
    initial = simulate_dca(bars, config())["results"]["conditional"]
    rolling = simulate_dca(
        bars, config(reference_mode="high_watermark"))["results"]["conditional"]
    assert not any(row["kind"] == "DIP" for row in initial["events"])
    dips = [row for row in rolling["events"] if row["kind"] == "DIP"]
    assert [row["drop_pct"] for row in dips] == ["0.1", "0.2"]
    assert all(row["signal_time"] == START + 2 * STEP for row in dips)
    assert all(row["execution_time"] == START + 3 * STEP for row in dips)
    assert "máximo de cierres ya observados" in simulate_dca(
        bars, config(reference_mode="high_watermark"))["assumptions"][1]
    with pytest.raises(LabError, match="Referencia"):
        config(reference_mode="future_high")


def test_invalid_rules_and_open_or_gapped_history_are_rejected(tmp_path):
    with pytest.raises(LabError, match="únicos"):
        config(dips=[{"drop_pct": "0.1", "amount": "50"}, {"drop_pct": "0.10", "amount": "60"}])
    with pytest.raises(LabError, match="rango"):
        config(amount="1e999999999")
    with pytest.raises(LabError, match="Exposición"):
        config(max_exposure="1.2")

    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    lab = LabStore(lab_path)
    bars = history()
    lab.save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    gap = lab.save_dataset("BTC/USDT", "1d", "gap", [bars[0], *bars[2:]],
                           [{"after": bars[0].t, "before": bars[2].t, "missing": 1}])
    store = AutomationStore(automation_path)
    rule = store.create_rule("Plan", config())
    app = FastAPI()
    app.include_router(create_automation_router(automation_path, lab_path))
    client = TestClient(app)
    assert client.post("/automation/dca/runs", json={"dataset_id": gap["id"], "version_id": rule["version_id"]}).status_code == 422
    assert client.get("/automation/dca/rules", headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_dca_api_versions_runs_and_reopen_without_paper_or_real(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    # Compatibilidad con una base DCA heredada sin cifrar.
    with AutomationStore(automation_path).connect():
        pass
    bars = history()
    dataset = LabStore(lab_path).save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    app = FastAPI()
    app.include_router(create_automation_router(automation_path, lab_path))
    client = TestClient(app)
    rule = client.post("/automation/dca/rules", json={"name": "Plan", "definition": config()}).json()
    assert rule["version"] == 1
    response = client.post("/automation/dca/runs", json={"dataset_id": dataset["id"], "version_id": rule["version_id"]})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["dataset_hash"] == dataset["hash"]
    assert run["definition_hash"] == content_hash(config())
    assert run["result_hash"]
    verified = client.get(f"/automation/dca/runs/{run['id']}/verify").json()
    assert verified["status"] == "verified" and all(verified["checks"].values())
    assert run["results"]["conditional"]["metrics"]["invested"] == "450"
    second = client.post("/automation/dca/rules/" + rule["id"] + "/versions",
                         json={"definition": config(amount="80")}).json()
    assert second["version"] == 2
    assert AutomationStore(automation_path).version(run["version_id"])["definition"]["amount"] == "100"
    reopened = FastAPI()
    reopened.include_router(create_automation_router(automation_path, lab_path))
    other_client = TestClient(reopened)
    assert other_client.get("/automation/dca/runs/" + run["id"]).json() == run
    assert other_client.get(f"/automation/dca/runs/{run['id']}/verify").json()["status"] == "verified"
    assert len(other_client.get("/automation/dca/rules").json()) == 1
    assert len(other_client.get("/automation/dca/runs").json()) == 1
    assert automation_path.read_bytes()[:16] == b"SQLite format 3\x00"  # Sólo ficción; sin secretos.


def test_dca_11_run_replays_after_motor_upgrade(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    bars = history()
    dataset = LabStore(lab_path).save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    store = AutomationStore(automation_path)
    version = store.create_rule("DCA legado", config())
    saved = store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                           simulate_dca(bars, version["definition"], engine_version="dca-1.1"), "dca-1.1",
                           lab_path=lab_path,
                           expected_definition_hash=content_hash(version["definition"]))
    app = FastAPI()
    app.include_router(create_automation_router(automation_path, lab_path))
    checked = TestClient(app).get(f"/automation/dca/runs/{saved['id']}/verify")
    assert checked.status_code == 200 and checked.json()["status"] == "verified"
    assert "reference_mode" not in version["definition"]


def test_dca_12_capped_lump_run_remains_verifiable(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    bars = history()
    dataset = LabStore(lab_path).save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    store = AutomationStore(automation_path)
    version = store.create_rule("DCA 1.2", config(monthly_limit="150"))
    saved = store.save_run(
        version["version_id"], dataset["id"], dataset["hash"],
        simulate_dca(bars, version["definition"], engine_version="dca-1.2"), "dca-1.2",
        lab_path=lab_path, expected_definition_hash=content_hash(version["definition"]))
    app = FastAPI()
    app.include_router(create_automation_router(automation_path, lab_path))
    checked = TestClient(app).get(f"/automation/dca/runs/{saved['id']}/verify")
    assert checked.status_code == 200 and checked.json()["status"] == "verified"


def test_dca_13_run_without_capital_return_remains_verifiable(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    bars = history()
    dataset = LabStore(lab_path).save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    store = AutomationStore(automation_path)
    version = store.create_rule("DCA 1.3", config(monthly_limit="150"))
    saved = store.save_run(
        version["version_id"], dataset["id"], dataset["hash"],
        simulate_dca(bars, version["definition"], engine_version="dca-1.3"), "dca-1.3",
        lab_path=lab_path, expected_definition_hash=content_hash(version["definition"]))
    app = FastAPI()
    app.include_router(create_automation_router(automation_path, lab_path))
    checked = TestClient(app).get(f"/automation/dca/runs/{saved['id']}/verify")
    assert checked.status_code == 200 and checked.json()["status"] == "verified"


def test_dca_14_run_replays_with_original_decimal_precision(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    bars = [Candle(START + i * STEP, Decimal("1"), Decimal("2"),
                   Decimal("0.5"), Decimal("1"), Decimal("1")) for i in range(3)]
    dataset = LabStore(lab_path).save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    store = AutomationStore(automation_path)
    version = store.create_rule("DCA 1.4", config(amount="1e-40", budget_total="9e40",
                                                   monthly_limit="9e40", dips=[]))
    legacy = simulate_dca(bars, version["definition"], engine_version="dca-1.4")
    assert legacy["results"]["fixed"]["metrics"]["remaining_cash"] == (
        "90000000000000000000000000000000000000000.000000000000000000000000000000000000000")
    saved = store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                           legacy, "dca-1.4", lab_path=lab_path,
                           expected_definition_hash=content_hash(version["definition"]))
    app = FastAPI()
    app.include_router(create_automation_router(automation_path, lab_path))
    checked = TestClient(app).get(f"/automation/dca/runs/{saved['id']}/verify")
    assert checked.status_code == 200 and checked.json()["status"] == "verified"


def test_dca_verifier_detects_changes_and_old_records(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    with AutomationStore(automation_path).connect():
        pass
    dataset = LabStore(lab_path).save_dataset("BTC/USDT", "1d", "fixture", history(), [])
    app = FastAPI()
    app.include_router(create_automation_router(automation_path, lab_path))
    client = TestClient(app)
    rule = client.post("/automation/dca/rules", json={"name": "Plan", "definition": config()}).json()

    def create_run():
        response = client.post("/automation/dca/runs", json={"dataset_id": dataset["id"],
                                                       "version_id": rule["version_id"]})
        assert response.status_code == 200, response.text
        return response.json()["id"]

    run_id = create_run()
    with sqlite3.connect(automation_path) as db:
        result = json.loads(db.execute("SELECT result_json FROM dca_runs WHERE id=?", (run_id,)).fetchone()[0])
        result["results"]["conditional"]["metrics"]["invested"] = "999"
        db.execute("UPDATE dca_runs SET result_json=? WHERE id=?", (json.dumps(result), run_id))
    check = client.get(f"/automation/dca/runs/{run_id}/verify").json()
    assert check["status"] == "mismatch" and check["checks"]["stored_result"] is False

    run_id = create_run()
    with sqlite3.connect(automation_path) as db:
        db.execute("UPDATE dca_runs SET engine_version='obsolete' WHERE id=?", (run_id,))
    assert client.get(f"/automation/dca/runs/{run_id}/verify").json()["status"] == "engine_unavailable"

    run_id = create_run()
    with sqlite3.connect(automation_path) as db:
        result = json.loads(db.execute("SELECT result_json FROM dca_runs WHERE id=?", (run_id,)).fetchone()[0])
        result.pop("definition_hash")
        result.pop("result_hash")
        db.execute("UPDATE dca_runs SET result_json=? WHERE id=?", (json.dumps(result), run_id))
    assert client.get(f"/automation/dca/runs/{run_id}/verify").json()["status"] == "legacy_unpinned"

    run_id = create_run()
    with sqlite3.connect(automation_path) as db:
        definition = json.loads(db.execute("SELECT definition_json FROM dca_versions WHERE id=?",
                                           (rule["version_id"],)).fetchone()[0])
        definition["amount"] = "1"
        db.execute("UPDATE dca_versions SET definition_json=? WHERE id=?",
                   (json.dumps(definition), rule["version_id"]))
    check = client.get(f"/automation/dca/runs/{run_id}/verify").json()
    assert check["status"] == "mismatch" and check["checks"]["definition"] is False

    with sqlite3.connect(lab_path) as db:
        db.execute("UPDATE datasets SET candles_json='[]' WHERE id=?", (dataset["id"],))
    check = client.get(f"/automation/dca/runs/{run_id}/verify").json()
    assert check["status"] == "mismatch" and check["checks"]["dataset"] is None


def test_dca_save_rejects_changed_inputs_between_calculation_and_persistence(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    lab, store = LabStore(lab_path), AutomationStore(automation_path)
    bars = history()
    dataset = lab.save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    version = store.create_rule("Plan", config())
    result = simulate_dca(bars, version["definition"])
    with pytest.raises(LabError, match="versión DCA cambió"):
        store.save_run(version["version_id"], dataset["id"], dataset["hash"], result, "dca-1.0",
                       lab_path=lab_path, expected_definition_hash=content_hash({"amount": "1"}))
    with sqlite3.connect(lab_path) as db:
        db.execute("UPDATE datasets SET candles_json='[]' WHERE id=?", (dataset["id"],))
    with pytest.raises(LabError, match="dataset cambió"):
        store.save_run(version["version_id"], dataset["id"], dataset["hash"], result, "dca-1.0",
                       lab_path=lab_path, expected_definition_hash=content_hash(version["definition"]))
    assert store.runs() == []


def test_dca_save_can_verify_encrypted_lab_attachment_without_leaking_key(tmp_path):
    lab_path, automation_path = tmp_path / "lab.db", tmp_path / "automation.db"
    lab, store = LabStore(lab_path), AutomationStore(automation_path)
    bars = history()
    dataset = lab.save_dataset("BTC/USDT", "1d", "fixture", bars, [])
    backup = create_secure_lab_backup(lab, "DCA attachment test password")
    encrypted_path = tmp_path / "lab-backups" / backup["name"]
    version = store.create_rule("Plan", config())
    result = simulate_dca(bars, version["definition"])
    kwargs = {"lab_path": encrypted_path,
              "expected_definition_hash": content_hash(version["definition"])}
    missing = tmp_path / "missing-lab.db"
    with pytest.raises(LabError, match="no está disponible"):
        store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                       result, "dca-1.0", lab_path=missing,
                       expected_definition_hash=kwargs["expected_definition_hash"])
    assert not missing.exists()
    with pytest.raises(LabError, match="contraseña o formato"):
        store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                       result, "dca-1.0", lab_passphrase="incorrect password", **kwargs)
    assert store.runs() == []
    saved = store.save_run(version["version_id"], dataset["id"], dataset["hash"],
                           result, "dca-1.0", lab_passphrase="DCA attachment test password", **kwargs)
    assert saved["dataset_hash"] == dataset["hash"]
    assert len(store.runs()) == 1
    assert b"DCA attachment test password" not in automation_path.read_bytes()
