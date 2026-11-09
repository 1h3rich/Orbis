"""UI → API LAB → SQLite temporal para un dataset PERP sin mezclar Spot."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8766"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    form = page.locator("#lab-download")
    form.wait_for()
    form.locator('[name="provider"]').select_option("grvt")
    expect(form.locator('[name="symbol"]')).to_have_value("BTC/USDT:PERP")
    expect(page.locator("#lab-download-market-note")).to_contain_text("sin financiación ni liquidación")
    form.locator('[name="start"]').fill("2025-01-01T00:00")
    form.locator('[name="end"]').fill("2025-01-01T03:00")
    form.locator('button[type="submit"]').click()
    expect(page.locator("#lab-notice")).to_contain_text("3 velas")
    expect(page.locator("#lab-dataset-status")).to_contain_text("grvt-perp-trade-public")
    expect(page.locator("#lab-dataset-status")).to_contain_text("sin huecos")
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#lab-dataset-status")).to_contain_text("grvt-perp-trade-public")
    expect(page.locator('#lab-download [name="provider"]')).to_have_value("grvt")
    expect(page.locator('#lab-download [name="symbol"]')).to_have_value("BTC/USDT:PERP")
    page.screenshot(path="/tmp/orbis-lab-grvt-history.png", full_page=True)
    assert not errors, errors
    print("LAB GRVT UI: fuente PERP → histórico completo → recarga OK")
    browser.close()
