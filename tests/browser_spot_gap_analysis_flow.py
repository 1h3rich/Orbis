"""UI Spot PAPER: hueco histórico visible sin crear compras ficticias."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8767"
START = 1_735_689_600
STEP = 3600

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/spot-paper")
    page.wait_for_load_state("networkidle")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#spot-paper-start").click()
    expect(page.locator("#spot-paper-detail")).to_contain_text("Caja ficticia")
    session_id = page.locator("#spot-paper-session").input_value()
    before = page.request.get(BASE + "/paper/spot/strategies/" + session_id).json()
    page.request.post(BASE + "/__test/advance_spot", data={"now": START + 5 * STEP + 10})
    page.wait_for_function("async id => { const r = await fetch('/paper/spot/strategies/' + id); return (await r.json()).status === 'GAP'; }", arg=session_id)
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#spot-paper-detail button[data-gap-analysis]")).to_be_visible()
    page.locator("#spot-paper-detail button[data-gap-analysis]").click()
    expect(page.locator("#spot-paper-gap-report")).to_contain_text("Reconstrucción")
    expect(page.locator("#spot-paper-gap-report")).to_contain_text("sin operaciones PAPER")
    expect(page.locator("#spot-paper-gap-report")).to_contain_text("guardada LAB")
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#spot-paper-gap-report")).to_contain_text("Última reconstrucción LAB")
    after = page.request.get(BASE + "/paper/spot/strategies/" + session_id).json()
    assert after["status"] == "GAP"
    assert after["state"] == before["state"]
    assert after["events"] == before["events"]
    assert not errors, errors
    browser.close()

print("Spot PAPER GAP: reconstrucción visible y estado ficticio intacto")
