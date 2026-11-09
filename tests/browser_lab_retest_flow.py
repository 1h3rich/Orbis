"""Ruptura y retesteo: edición, backtest, marcas y recuperación local."""

from datetime import datetime, timezone

from playwright.sync_api import sync_playwright


start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
ohlc = [(99, 101, 98, 99), (99, 100, 97, 98), (98, 102, 97, 100),
        (101, 105, 100, 104), (104, 105, 101.5, 103),
        (106, 108, 105, 107), (107, 109, 107, 108)]
candles = [
    {"t": start + i * 3600, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": "10"}
    for i, (o, h, l, c) in enumerate(ohlc)
]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    response = page.request.post("http://127.0.0.1:8799/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "browser-retest-fixture", "candles": candles,
    })
    assert response.ok, response.text()
    page.goto("http://127.0.0.1:8799/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-strategy-form [name="name"]').fill("Ruptura y retesteo")
    page.locator('#lab-rule-list [name="kind"]').select_option("RETEST_ABOVE_RANGE")
    page.locator('#lab-rule-list [name="period"]').fill("3")
    page.locator('#lab-rule-list [name="retest_bars"]').fill("3")
    page.locator('#lab-rule-list [name="tolerance_pct"]').fill("0.01")
    assert page.locator('#lab-rule-list [name="retest_bars"]').is_visible()
    assert not page.locator('#lab-rule-list [name="value"]').is_visible()
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-chart canvas").first.wait_for()
    assert page.locator("#lab-chart").get_attribute("data-signal-count") == "1"
    assert page.locator("#lab-chart").get_attribute("data-entry-count") == "1"
    page.locator("#lab-report details summary").click()
    assert "RETEST_ABOVE_RANGE" in page.locator("#lab-report").inner_text()
    page.screenshot(path="/tmp/orbis-lab-retest.png", full_page=True)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    assert page.locator('#lab-rule-list [name="kind"]').input_value() == "RETEST_ABOVE_RANGE"
    assert page.locator('#lab-rule-list [name="tolerance_pct"]').input_value() == "0.01"
    page.locator("#lab-run-select").select_option(index=1)
    page.locator("#lab-chart canvas").first.wait_for()
    assert not errors, errors
    print("Browser LAB retest flow passed: editor, structure, markers, persistence")
    browser.close()
