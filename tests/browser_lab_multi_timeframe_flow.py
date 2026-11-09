"""El editor guarda, ejecuta y reabre una regla 4h sobre histórico 1h."""

from datetime import datetime, timezone

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
rows = []
for index in range(16):
    close = 10 + index // 4 * 10
    rows.append({"t": start + index * 3600, "o": str(close),
                 "h": str(close + 1), "l": str(close - 1),
                 "c": str(close), "v": "1"})

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    response = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "Multitemporal fixture",
        "candles": rows})
    assert response.ok, response.text()
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-strategy-form [name="name"]').fill("1h con filtro 4h")
    first = page.locator("#lab-rule-list .lab-rule").first
    first.locator('[name="kind"]').select_option("RSI_GT")
    first.locator('[name="period"]').fill("2")
    first.locator('[name="value"]').fill("30")
    page.locator("#lab-add-rule").click()
    second = page.locator("#lab-rule-list .lab-rule").nth(1)
    second.locator('[name="kind"]').select_option("CLOSE_ABOVE_SMA")
    second.locator('[name="period"]').fill("2")
    second.locator('[name="rule_timeframe"]').select_option("4h")
    page.locator('#lab-strategy-form [name="stop_loss"]').fill("0.9")
    page.locator('#lab-strategy-form [name="take_profit"]').fill("9")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    version = page.request.get(BASE + "/lab/strategies").json()[0]
    assert version["definition"]["entry"]["children"][1] == {
        "kind": "CLOSE_ABOVE_SMA", "period": 2, "timeframe": "4h"}
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    run = page.request.get(BASE + "/lab/runs").json()[0]
    assert run["engine_version"] == "lab-1.16"
    assert run["metrics"]["sample_size"] > 0
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    assert page.locator("#lab-rule-list .lab-rule").nth(1).locator(
        '[name="rule_timeframe"]').input_value() == "4h"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path="/tmp/orbis-lab-multi-timeframe.png", full_page=True)
    assert not errors, errors
    browser.close()
    print("LAB multi-timeframe: save, backtest, verify and reopen passed")
