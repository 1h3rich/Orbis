"""GRVT PAPER identifica una condición superior aún no evaluable."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8766"
definition = {"direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
              "fee_rate": "0.001", "slippage_rate": "0", "stop_loss": "0.03",
              "take_profit": "0.08", "entry": {"kind": "CLOSE_ABOVE_EMA",
                                               "period": 2, "timeframe": "4h"}}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    created = page.request.post(BASE + "/lab/strategies", data={
        "name": "EMA 4h cobertura GRVT", "definition": definition})
    assert created.ok, created.text()
    page.goto(BASE + "/ui/#/perpetuos")
    page.wait_for_load_state("networkidle")
    page.locator("#perp-account-form button[type=submit]").click()
    expect(page.locator("#perp-summary")).to_contain_text("10000 USDT")
    page.locator('#perp-signal-form [name="version_id"]').select_option(
        value=created.json()["version_id"])
    page.locator("#perp-signal-create").click()
    expect(page.locator("#perp-signal-status")).to_contain_text("Seguimiento iniciado")
    page.locator('[data-signal-action="inspect"]').click()
    chip = page.locator('#perp-signal-detail .paper-coverage-chip[data-status="INSUFFICIENT"]')
    expect(chip).to_contain_text("Entrada · 4h 1/2 · calentando")
    page.locator('#perp-signal-detail [data-signal-action="verify"]').click()
    expect(page.locator('#perp-signal-origin[data-status="verified"]')).to_have_text("Origen verificado")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator('[data-signal-action="inspect"]').click()
    expect(chip).to_contain_text("Entrada · 4h 1/2 · calentando")
    page.locator('#perp-signal-detail [data-signal-action="verify"]').click()
    expect(page.locator('#perp-signal-origin[data-status="verified"]')).to_have_text("Origen verificado")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    browser.close()
    print("GRVT PAPER: calentamiento y origen LAB verificado tras recarga")
