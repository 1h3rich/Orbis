"""Smoke LAB: verificación visible, alteración detectada y gráficos bloqueados."""

import os
import sqlcipher3

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
START = 1735689600
rows = [{"t": START + i * 3600, "o": str(10 + i), "h": str(11 + i),
         "l": str(9 + i), "c": str(10 + i), "v": "10"} for i in range(8)]
definition = {"direction": "LONG", "initial_capital": "1000", "allocation": "1",
              "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
              "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}

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
    run_response = page.request.post(BASE + "/lab/runs", data={
        "dataset_id": dataset["id"], "version_id": strategy["version_id"],
    })
    assert run_response.status == 200, run_response.text()
    run = run_response.json()

    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run["id"])
    page.locator("#lab-chart canvas").first.wait_for()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    assert "REPRODUCIBLE" in page.locator("#lab-run-verify").inner_text()
    assert page.locator("#lab-chart canvas").count() > 0

    with sqlcipher3.connect(os.environ["ORBIS_LAB_PATH"]) as db:
        test_passphrase = os.environ.get("ORBIS_LAB_TEST_PASSPHRASE")
        if test_passphrase:
            db.execute("PRAGMA key = '" + test_passphrase.replace("'", "''") + "'")
        db.execute("UPDATE detected_events SET event_json='{}' WHERE run_id=? AND index_no=0", (run["id"],))
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "mismatch")
    expect(page.locator("#lab-chart")).to_contain_text("Gráfico bloqueado")
    assert page.locator("#lab-chart canvas").count() == 0

    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run["id"])
    expect(page.locator("#lab-report")).to_contain_text("Prueba no reproducible")
    assert page.locator("#lab-report .lab-metrics").count() == 0
    page.get_by_role("button", name="Repetir como prueba nueva").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    new_id = page.locator("#lab-run-select").input_value()
    assert new_id != run["id"]
    new_run = page.request.get(BASE + "/lab/runs/" + new_id + "?require_verified=true")
    assert new_run.status == 200 and new_run.json()["replay_of_run_id"] == run["id"]
    assert page.request.get(BASE + "/lab/runs/" + run["id"] + "/verify").json()["status"] == "mismatch"
    assert not errors, errors
    print("Browser LAB verification passed: tamper, blocked history, explicit new replay and lineage")
    browser.close()
