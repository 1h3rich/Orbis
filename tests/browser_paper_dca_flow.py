"""Recorrido UI de DCA PAPER: activar, esperar, observar, recuperar y detener."""

import os

from playwright.sync_api import expect, sync_playwright


START = 1_735_689_600
DAY = 86400
URL = "http://127.0.0.1:8801"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(URL + "/ui/#/dca")
    page.wait_for_load_state("networkidle")
    page.locator("#dca-rule").select_option(index=1)
    expect(page.locator("#dca-version")).not_to_have_value("")
    page.locator("#dca-run").click()
    page.get_by_text("Comparación guardada.").wait_for()
    page.locator("#dca-paper-dataset").select_option(index=1)
    expect(page.locator("#dca-paper-run")).not_to_have_value("")
    chosen_run = page.locator("#dca-paper-run").input_value()
    page.locator("#dca-paper-start").click()
    page.locator("#dca-paper-report .statusbadge.on").wait_for()
    assert page.request.get(URL + "/automation/dca/paper/sessions").json()[0]["source_run_id"] == chosen_run
    expect(page.locator("#dca-paper-start")).to_be_disabled()
    expect(page.locator("#dca-paper-start")).to_have_text("Ya activo")
    page.get_by_text("HISTÓRICO · VERIFICADO").wait_for()
    assert "origen fijado" in page.locator("#dca-paper-comparison").inner_text()
    assert "periodos y ejecución distintos" in page.locator("#dca-paper-comparison").inner_text()
    page.get_by_text("OBSERVADOR · ACTIVO").wait_for()
    assert "ACTIVE" in page.locator("#dca-paper-report").inner_text()
    page.locator("#dca-paper-poll").click()
    page.get_by_text("Estado: WAIT").wait_for()
    advanced = page.request.post(URL + "/__test/advance_paper_dca", data={"now": START + 4 * DAY + 1})
    assert advanced.ok
    expect(page.locator("#dca-paper-report tbody tr")).to_have_count(3, timeout=20000)
    assert "BUY" in page.locator("#dca-paper-report tbody").inner_text()
    assert "compras" in page.locator("#dca-paper-comparison").inner_text()
    if os.environ.get("ORBIS_BROWSER_SCREENSHOT"):
        page.screenshot(path=os.environ["ORBIS_BROWSER_SCREENSHOT"], full_page=True)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#dca-paper-session").select_option(index=1)
    page.get_by_text("OBSERVATION", exact=True).wait_for()
    page.locator("#dca-paper-stop").click()
    page.locator("#dca-paper-report .statusbadge").filter(has_text="STOPPED").wait_for()
    page.locator("#dca-rule").select_option(index=1)
    expect(page.locator("#dca-version")).not_to_have_value("")
    page.locator("#dca-paper-dataset").select_option(index=1)
    expect(page.locator("#dca-paper-start")).to_be_enabled()
    assert not errors, errors
    print("Browser PAPER DCA passed: active, wait, buy, recovery, stop")
    browser.close()
