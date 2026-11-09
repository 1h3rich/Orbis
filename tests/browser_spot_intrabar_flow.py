"""Stop PAPER observado entre cierres: diario inmediato y marca al cerrar vela."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8767"
START = 1_735_689_600
STEP = 3600

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/spot-paper")
    page.wait_for_load_state("networkidle")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#spot-paper-start").click()
    expect(page.locator("#spot-paper-notice")).to_contain_text("Sesión ficticia activada")
    page.wait_for_function("() => !!document.querySelector('#spot-paper-session').value")
    session_id = page.locator("#spot-paper-session").input_value()

    page.request.post(BASE + "/__test/advance_spot", data={"now": START + 4 * STEP + 1})
    page.wait_for_function("async id => { const r = await fetch('/paper/spot/strategies/' + id); return (await r.json()).events.length === 1; }", arg=session_id)
    page.locator("#spot-paper-poll").click()
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-buy-count", "1")

    page.request.post(BASE + "/__test/advance_spot", data={
        "now": START + 4 * STEP + 6, "bid": "96", "ask": "97",
    })
    page.wait_for_function("async id => { const r = await fetch('/paper/spot/strategies/' + id); return (await r.json()).state.closed_trades === 1; }", arg=session_id)
    page.locator("#spot-paper-poll").click()
    expect(page.locator("#spot-paper-detail")).to_contain_text("Stop · bid observado")
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-sell-count", "0")

    page.request.post(BASE + "/__test/advance_spot", data={"now": START + 5 * STEP + 1})
    page.wait_for_function("async id => { const r = await fetch('/paper/spot/strategies/' + id); return (await r.json()).events.length === 3; }", arg=session_id)
    page.locator("#spot-paper-poll").click()
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-sell-count", "1")
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#spot-paper-detail")).to_contain_text("Stop · bid observado")
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-sell-count", "1")
    assert not errors, errors
    browser.close()
    print("Spot PAPER intrabar browser flow passed")
