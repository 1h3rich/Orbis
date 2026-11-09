"""Recorrido LAB real sobre una base temporal; no usa datos financieros del usuario."""

from playwright.sync_api import expect, sync_playwright


start = 1735689600
csv = "time,open,high,low,close,volume\n" + "\n".join(
    f"{start + i * 3600},{10+i},{11+i},{9+i},{10+i},10" for i in range(8)
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-csv [name="file"]').set_input_files({"name": "velas.csv", "mimeType": "text/csv", "buffer": csv.encode()})
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-dataset option:not([value=""])').first.wait_for(state="attached")
    page.locator('#lab-strategy-form [name="name"]').fill("EMA de prueba")
    page.locator('#lab-strategy-form [name="period"]').first.fill("2")
    page.locator('#lab-strategy-form [name="exit_enabled"]').check()
    page.locator('#lab-exit-rule-list [name="kind"]').select_option("RSI_GT")
    page.locator('#lab-exit-rule-list [name="period"]').fill("2")
    page.locator('#lab-exit-rule-list [name="value"]').fill("60")
    page.locator('#lab-strategy-form [name="stop_loss"]').fill("0.5")
    page.locator('#lab-strategy-form [name="take_profit"]').fill("0.9")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator('#lab-strategy option:not([value=""])').first.wait_for(state="attached")
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run").click()
    expect(page.locator("#lab-notice")).to_contain_text("Prueba guardada")
    page.locator("#lab-chart canvas").first.wait_for()
    assert int(page.locator("#lab-chart").get_attribute("data-signal-count")) > 0
    assert int(page.locator("#lab-chart").get_attribute("data-entry-count")) > 0
    assert page.locator("#lab-report tbody tr").count() >= 1
    page.locator("#lab-report tbody tr").filter(has_text="Regla de salida").first.wait_for()
    page.locator("#lab-equity-range select").select_option("5")
    expect(page.locator("#lab-equity-range")).to_have_attribute("data-view", "5")
    page.screenshot(path="/tmp/orbis-lab-flow.png", full_page=True)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run-select").select_option(index=1)
    page.locator("#lab-chart canvas").first.wait_for()
    expect(page.locator("#lab-equity-range")).to_have_attribute("data-view", "5")
    assert page.locator('#lab-strategy-form [name="exit_enabled"]').is_checked()
    page.locator("#lab-report tbody tr").filter(has_text="Regla de salida").first.wait_for()
    assert "No crea órdenes" in page.locator("#lab-run").get_attribute("title")
    page.goto("http://127.0.0.1:8799/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").wait_for()
    page.get_by_text("Datos locales y cookies").click()
    page.get_by_text("REAL usa SQLCipher", exact=False).wait_for()
    assert page.context.cookies() == []
    page.goto("http://127.0.0.1:8799/ui/#/estudio")
    page.locator(".estudio-cabecera h2").get_by_text("Estudio", exact=True).wait_for()
    assert "PAPER" in page.locator("#st-buy-open").get_attribute("title")
    assert page.locator("#st-save").inner_text() == "Guardar"
    assert not errors, errors
    print("Browser LAB flow passed: CSV, exit rule, backtest, chart, persistence, settings, estudio")
    browser.close()
