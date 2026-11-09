"""Smoke de verificación visible para holdout y búsqueda de parámetros."""

import json
import os
import sqlite3

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
START = 1735689600
rows = [{"t": START + i * 3600, "o": str(100 + i), "h": str(102 + i),
         "l": str(99 + i), "c": str(101 + i), "v": "10"} for i in range(200)]
definition = {"direction": "LONG", "initial_capital": "1000", "allocation": "0.2",
              "fee_rate": "0.001", "slippage_rate": "0.001", "stop_loss": "0.03",
              "take_profit": "0.08", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 10}}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    dataset_response = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "fixture aislado", "candles": rows,
    })
    assert dataset_response.status == 200, dataset_response.text()
    dataset = dataset_response.json()
    strategy_response = page.request.post(BASE + "/lab/strategies", data={
        "name": "EMA de verificación", "definition": definition,
    })
    assert strategy_response.status == 200, strategy_response.text()
    strategy = strategy_response.json()
    base = {"dataset_id": dataset["id"], "version_id": strategy["version_id"],
            "train_fraction": "0.7"}
    validation = page.request.post(BASE + "/lab/validations", data=base).json()
    optimization = page.request.post(BASE + "/lab/optimizations", data={
        **base, "stop_losses": ["0.02", "0.04"], "take_profits": ["0.05", "0.1"],
    }).json()

    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-validation-select").select_option(validation["id"])
    page.locator("#lab-validation-report").get_by_role("button", name="Verificar", exact=True).click()
    expect(page.locator("#lab-validation-report .lab-verify")).to_have_attribute("data-state", "verified")
    page.locator("#lab-opt-select").select_option(optimization["id"])
    page.locator("#lab-opt-report .lab-verify button").click()
    expect(page.locator("#lab-opt-report .lab-verify")).to_have_attribute("data-state", "verified")

    with sqlite3.connect(os.environ["ORBIS_LAB_PATH"]) as db:
        result = json.loads(db.execute("SELECT result_json FROM optimization_runs WHERE id=?",
                                       (optimization["id"],)).fetchone()[0])
        result["candidates"][0]["train_metrics"]["net_profit"] = "999999"
        db.execute("UPDATE optimization_runs SET result_json=? WHERE id=?",
                   (json.dumps(result), optimization["id"]))
    page.locator("#lab-opt-report .lab-verify button").click()
    expect(page.locator("#lab-opt-report .lab-verify")).to_have_attribute("data-state", "mismatch")
    assert page.locator("#lab-opt-report").get_attribute("data-integrity") == "mismatch"
    assert not errors, errors
    print("Browser LAB experiment verification passed: holdout, grid, tamper alert")
    browser.close()
