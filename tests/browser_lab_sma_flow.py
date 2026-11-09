"""SMA real en el editor: versión, señal, repetición y recuperación."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
start = 1_735_689_600
prices = (10, 12, 14, 10, 8, 11, 15, 16)
rows = [{"t": start + i * 3600, "o": str(p), "h": str(p + 1),
         "l": str(p - 1), "c": str(p), "v": "10"} for i, p in enumerate(prices)]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    response = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "SMA fixture", "candles": rows})
    assert response.ok, response.text()
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-strategy-form [name="name"]').fill("SMA causal")
    page.locator('#lab-rule-list [name="kind"]').first.select_option("CLOSE_ABOVE_SMA")
    page.locator('#lab-rule-list [name="period"]').first.fill("3")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-chart canvas").first.wait_for()
    assert int(page.locator("#lab-chart").get_attribute("data-signal-count")) > 0
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    strategy = page.request.get(BASE + "/lab/strategies").json()[0]
    assert strategy["definition"]["entry"]["children"][0] == {
        "kind": "CLOSE_ABOVE_SMA", "period": 3}
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    assert page.locator('#lab-rule-list [name="kind"]').first.input_value() == "CLOSE_ABOVE_SMA"
    assert page.locator('#lab-rule-list [name="period"]').first.input_value() == "3"
    assert not errors, errors
    print("Browser LAB SMA passed: strategy, markers, replay, reopen")
    browser.close()
