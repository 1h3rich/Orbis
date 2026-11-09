"""Recorrido LAB fuera de muestra sobre una base y CSV temporales."""

import json
import os
import sqlite3

from playwright.sync_api import expect, sync_playwright


START = 1_735_689_600
BASE = "http://127.0.0.1:8798"
csv = "time,open,high,low,close,volume\n" + "\n".join(
    f"{START + i * 3600},{100+i},{102+i},{99+i},{101+i},{10+i}"
    for i in range(100)
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-csv [name="file"]').set_input_files(
        {"name": "holdout.csv", "mimeType": "text/csv", "buffer": csv.encode()})
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-strategy-form [name="name"]').fill("EMA cronológica")
    page.locator('#lab-strategy-form [name="period"]').first.fill("10")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    expect(page.locator("#lab-report")).to_contain_text("Volatilidad/vela")
    page.locator("#lab-validation-fraction").select_option("0.7")
    page.locator("#lab-validate").click()
    page.get_by_text("Validación cronológica guardada", exact=False).wait_for()
    expect(page.locator("#lab-validation-report")).to_contain_text("DESARROLLO · 70")
    expect(page.locator("#lab-validation-report")).to_contain_text("RESERVA · 30")
    assert page.locator("#lab-validation-report .lab-validation-table tbody tr").count() == 6
    expect(page.locator("#lab-validation-report")).to_contain_text("Volatilidad/vela")
    expect(page.locator("#lab-validation-report .lab-reserve-reuse")).to_contain_text("SIN SOLAPE REGISTRADO")
    validation_id = page.locator("#lab-validation-select").input_value()
    response = page.request.get(BASE + "/lab/validations/" + validation_id)
    assert response.ok
    result = response.json()
    assert result["split_index"] == 70
    assert result["risk"]["train"]["version"] == "equity-risk-1.1"
    assert all(row["signal_time"] >= result["test_start_t"]
               for row in result["out_of_sample"]["events"])
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-validation-select").select_option(validation_id)
    expect(page.locator("#lab-validation-report")).to_contain_text("RESERVA · 30")
    page.locator("#lab-strategy").select_option(index=1)
    expect(page.locator("#lab-version option")).to_have_count(1)
    page.locator("#lab-rolling-folds").select_option("2")
    page.locator("#lab-rolling").click()
    page.get_by_text("Ventanas guardadas", exact=False).wait_for()
    expect(page.locator("#lab-validation-report .lab-validation-table tbody tr")).to_have_count(2)
    rolling_id = page.locator("#lab-validation-select").input_value()
    rolling = page.request.get(BASE + "/lab/validations/" + rolling_id).json()
    assert rolling["method"] == "ROLLING_FIXED_VERSION"
    assert rolling["reserve_reuse"]["overlapping_run_count"] == 1
    expect(page.locator("#lab-validation-report .lab-reserve-reuse")).to_contain_text("RESERVA COMPARTIDA")
    assert rolling["windows"][0]["test_end_t"] < rolling["windows"][1]["test_start_t"]
    page.locator("#lab-validation-report").get_by_role("button", name="Verificar", exact=True).click()
    expect(page.locator("#lab-validation-report .lab-verify")).to_have_attribute("data-state", "verified")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-validation-select").select_option(rolling_id)
    expect(page.locator("#lab-validation-report .lab-validation-table tbody tr")).to_have_count(2)
    page.locator("#lab-validation-select").select_option(validation_id)
    expect(page.locator("#lab-validation-report .lab-reserve-reuse")).to_contain_text("RESERVA COMPARTIDA")
    page.locator("#lab-validation-select").select_option(rolling_id)
    with sqlite3.connect(os.environ["ORBIS_LAB_PATH"]) as db:
        result = json.loads(db.execute("SELECT result_json FROM validation_runs WHERE id=?", (rolling_id,)).fetchone()[0])
        result["windows"][0]["out_of_sample"]["metrics"]["net_profit"] = "999999"
        db.execute("UPDATE validation_runs SET result_json=? WHERE id=?", (json.dumps(result), rolling_id))
    page.locator("#lab-validation-report").get_by_role("button", name="Verificar", exact=True).click()
    expect(page.locator("#lab-validation-report .lab-verify")).to_have_attribute("data-state", "mismatch")
    expect(page.locator("#lab-validation-report .lab-rolling-sensitive")).to_be_hidden()
    page.screenshot(path="/tmp/orbis-lab-validation.png", full_page=True)
    assert not errors, errors
    print("LAB holdout + ventanas: persistencia, repetición y bloqueo de cifras alteradas OK")
    browser.close()
