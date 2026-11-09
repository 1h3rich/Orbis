"""Bollinger en entrada/salida LAB: persistencia y verificación visible."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
start = 1_735_689_600
prices = (10, 10, 20, 20, 10, 10, 20, 20)
rows = [{"t": start + i * 3600, "o": str(p), "h": str(p + 1),
         "l": str(p - 1), "c": str(p), "v": "10"} for i, p in enumerate(prices)]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    imported = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "Bollinger fixture", "candles": rows})
    assert imported.ok, imported.text()
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-strategy-form [name="name"]').fill("Bandas causales")
    page.locator('#lab-rule-list [name="kind"]').first.select_option("CLOSE_ABOVE_BB_UPPER")
    page.locator('#lab-rule-list [name="period"]').first.fill("3")
    page.locator('#lab-rule-list [name="value"]').first.fill("1")
    expect(page.locator('#lab-rule-list [name="value"]').first.locator("xpath=..")).to_contain_text("Mult. σ")
    page.locator('[name="exit_enabled"]').check()
    page.locator('#lab-exit-rule-list [name="kind"]').first.select_option("CLOSE_BELOW_BB_LOWER")
    page.locator('#lab-exit-rule-list [name="period"]').first.fill("3")
    page.locator('#lab-exit-rule-list [name="value"]').first.fill("1")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-chart canvas").first.wait_for()
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    strategy = page.request.get(BASE + "/lab/strategies").json()[0]["definition"]
    assert strategy["entry"]["children"][0] == {"kind": "CLOSE_ABOVE_BB_UPPER", "period": 3,
                                                    "multiplier": "1"}
    assert strategy["exit"]["children"][0] == {"kind": "CLOSE_BELOW_BB_LOWER", "period": 3,
                                                   "multiplier": "1"}
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    assert page.locator('#lab-rule-list [name="kind"]').first.input_value() == "CLOSE_ABOVE_BB_UPPER"
    assert page.locator('#lab-rule-list [name="value"]').first.input_value() == "1"
    assert page.locator('#lab-exit-rule-list [name="kind"]').first.input_value() == "CLOSE_BELOW_BB_LOWER"
    assert not errors, errors
    print("Browser LAB Bollinger passed: entry, exit, markers, replay, reopen")
    browser.close()
