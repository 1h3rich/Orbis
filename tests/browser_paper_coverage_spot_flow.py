"""Spot PAPER muestra calentamiento multitemporal sin fingir una señal."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8767"
START = 1_735_689_600
STEP = 3600
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
        "name": "EMA 4h cobertura", "definition": definition})
    assert created.ok, created.text()
    page.goto(BASE + "/ui/#/spot-paper")
    page.wait_for_load_state("networkidle")
    page.locator("#spot-paper-strategy").select_option(label="EMA 4h cobertura")
    expect(page.locator("#spot-paper-version")).to_contain_text("v1")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#spot-paper-start").click()
    chip = page.locator('#spot-paper-detail .paper-coverage-chip[data-status="INSUFFICIENT"]')
    expect(chip).to_contain_text("Entrada · 4h 0/2 · calentando")
    assert "No equivale a una señal fallida" in chip.get_attribute("data-help")
    page.locator("#spot-paper-verify").click()
    expect(page.locator('#spot-paper-origin[data-status="verified"]')).to_have_text("Origen verificado")
    session_id = page.locator("#spot-paper-session").input_value()
    page.request.post(BASE + "/__test/advance_spot", data={"now": START + 4 * STEP + 10})
    page.wait_for_function("async id => { const r = await fetch('/paper/spot/strategies/' + id); "
                           "return (await r.json()).coverage.rules[0].complete_bars === 1; }",
                           arg=session_id)
    page.locator("#spot-paper-poll").click()
    expect(chip).to_contain_text("Entrada · 4h 1/2 · calentando")
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(chip).to_contain_text("Entrada · 4h 1/2 · calentando")
    page.locator("#spot-paper-verify").click()
    expect(page.locator('#spot-paper-origin[data-status="verified"]')).to_have_text("Origen verificado")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    browser.close()
    print("Spot PAPER: calentamiento y origen LAB verificado tras recarga")
