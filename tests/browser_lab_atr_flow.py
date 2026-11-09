"""ATR en el editor LAB: CSV → versión → backtest → recuperación (BD temporal)."""

from playwright.sync_api import sync_playwright


start = 1_735_689_600
csv = "time,open,high,low,close,volume\n" + "\n".join(
    f"{start + i * 3600},{100+i},{110+i},{90+i},{100+i},10" for i in range(12)
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/laboratorio")
    page.locator('#lab-csv [name="file"]').set_input_files({
        "name": "atr.csv", "mimeType": "text/csv", "buffer": csv.encode()})
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-dataset option:not([value=""])').first.wait_for(state="attached")
    page.locator('#lab-strategy-form [name="name"]').fill("ATR causal")
    page.locator('#lab-rule-list [name="kind"]').first.select_option("ATR_PCT_GT")
    page.locator('#lab-rule-list [name="period"]').first.fill("3")
    page.locator('#lab-rule-list [name="value"]').first.fill("10")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator('#lab-strategy option:not([value=""])').first.wait_for(state="attached")
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-chart canvas").first.wait_for()
    assert int(page.locator("#lab-chart").get_attribute("data-signal-count")) > 0
    saved = page.request.get("http://127.0.0.1:8799/lab/strategies").json()
    assert saved[0]["definition"]["entry"]["children"][0] == {
        "kind": "ATR_PCT_GT", "period": 3, "threshold": "10"}
    page.reload()
    page.locator("#lab-strategy").select_option(index=1)
    assert page.locator('#lab-rule-list [name="kind"]').first.input_value() == "ATR_PCT_GT"
    assert page.locator('#lab-rule-list [name="period"]').first.input_value() == "3"
    assert page.locator('#lab-rule-list [name="value"]').first.input_value() == "10"
    assert not errors, errors
    print("LAB ATR flow passed: versioned rule, backtest markers and recovery")
    browser.close()
