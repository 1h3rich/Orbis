"""Editor EMA rápida/lenta → backtest → marcas → recuperación."""

from playwright.sync_api import sync_playwright


START = 1735689600
PRICES = (10, 10, 10, 9, 8, 9, 10, 11, 10, 9, 8)
CSV = "time,open,high,low,close,volume\n" + "\n".join(
    f"{START + i * 3600},{price},{price + 1},{price - 1},{price},10"
    for i, price in enumerate(PRICES)
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-csv [name="file"]').set_input_files({
        "name": "ema.csv", "mimeType": "text/csv", "buffer": CSV.encode(),
    })
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-strategy-form [name="name"]').fill("Cruce EMA")
    entry = page.locator("#lab-rule-list .lab-rule").first
    entry.locator('[name="kind"]').select_option("EMA_CROSS_ABOVE_EMA")
    entry.locator('[name="period"]').fill("2")
    entry.locator('[name="slow_period"]').fill("3")
    assert entry.locator('[name="slow_period"]').is_visible()
    assert not entry.locator('[name="signal_period"]').is_visible()
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-chart canvas").first.wait_for()
    assert int(page.locator("#lab-chart").get_attribute("data-signal-count")) == 1
    assert int(page.locator("#lab-chart").get_attribute("data-entry-count")) == 1
    run_id = page.locator("#lab-run-select").input_value()
    run = page.request.get(f"http://127.0.0.1:8799/lab/runs/{run_id}").json()
    assert run["engine_version"] == "lab-1.9"
    assert run["events"][0]["signal_time"] == START + 6 * 3600
    assert run["events"][0]["entry_time"] == START + 7 * 3600
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    assert page.locator('#lab-rule-list .lab-rule [name="kind"]').first.input_value() == "EMA_CROSS_ABOVE_EMA"
    assert page.locator('#lab-rule-list .lab-rule [name="slow_period"]').first.input_value() == "3"
    page.locator("#lab-run-select").select_option(run_id)
    page.locator("#lab-chart canvas").first.wait_for()
    assert not errors, errors
    browser.close()
    print("LAB EMA cross: versión, señal causal, entrada y recuperación")
