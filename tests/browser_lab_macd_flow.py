"""MACD: edición, simulación, señal visible, verificación y reapertura."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
start = 1_735_689_600
prices = [10, 10, 10, 10, 10, 12, 14, 16, 18, 17, 15, 13, 11, 9, 11, 13, 15, 17]
rows = [{"t": start + i * 3600, "o": str(p), "h": str(p + 1),
         "l": str(p - 1), "c": str(p), "v": "10"} for i, p in enumerate(prices)]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    imported = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "MACD fixture", "candles": rows})
    assert imported.ok, imported.text()
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-strategy-form [name="name"]').fill("MACD causal")
    entry = page.locator("#lab-rule-list .lab-rule").first
    entry.locator('[name="kind"]').select_option("MACD_CROSS_ABOVE_SIGNAL")
    expect(entry.locator('[name="period"]')).to_have_value("12")
    expect(entry.locator('[name="slow_period"]')).to_have_value("26")
    expect(entry.locator('[name="signal_period"]')).to_have_value("9")
    entry.locator('[name="period"]').fill("2")
    entry.locator('[name="slow_period"]').fill("3")
    entry.locator('[name="signal_period"]').fill("2")
    page.locator('[name="exit_enabled"]').check()
    exit_rule = page.locator("#lab-exit-rule-list .lab-rule").first
    exit_rule.locator('[name="kind"]').select_option("MACD_CROSS_BELOW_SIGNAL")
    exit_rule.locator('[name="period"]').fill("2")
    exit_rule.locator('[name="slow_period"]').fill("3")
    exit_rule.locator('[name="signal_period"]').fill("2")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-chart canvas").first.wait_for()
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    strategy = page.request.get(BASE + "/lab/strategies").json()[0]["definition"]
    assert strategy["entry"]["children"][0] == {
        "kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 2, "slow_period": 3, "signal_period": 2}
    assert strategy["exit"]["children"][0] == {
        "kind": "MACD_CROSS_BELOW_SIGNAL", "period": 2, "slow_period": 3, "signal_period": 2}
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    expect(page.locator('#lab-rule-list [name="kind"]').first).to_have_value("MACD_CROSS_ABOVE_SIGNAL")
    expect(page.locator('#lab-rule-list [name="slow_period"]').first).to_have_value("3")
    expect(page.locator('#lab-exit-rule-list [name="kind"]').first).to_have_value("MACD_CROSS_BELOW_SIGNAL")
    assert not errors, errors
    browser.close()
    print("Browser LAB MACD passed: edit, run, marks, replay, reopen")
