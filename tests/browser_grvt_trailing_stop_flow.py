"""LAB → GRVT PAPER: stop móvil SHORT, marca de motivo y reinicio."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8766"
START = 1_735_689_600
STEP = 3600

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    strategy = page.request.post(BASE + "/lab/strategies", data={
        "name": "Stop móvil GRVT",
        "definition": {"direction": "SHORT", "initial_capital": "1000", "allocation": "0.1",
                       "fee_rate": "0.001", "slippage_rate": "0", "stop_loss": "0.2",
                       "take_profit": "0.8", "trailing_stop": "0.05",
                       "entry": {"kind": "BEARISH_STREAK", "period": 3}},
    })
    assert strategy.ok, strategy.text()
    page.goto(BASE + "/ui/#/perpetuos")
    page.wait_for_load_state("networkidle")
    page.locator("#perp-account-form button[type=submit]").click()
    expect(page.locator("#perp-summary")).to_contain_text("10000 USDT")
    page.locator('#perp-signal-form [name="version_id"]').select_option(strategy.json()["version_id"])
    page.locator('#perp-signal-form [name="execution_mode"]').select_option("AUTO_PAPER")
    expect(page.locator("#perp-auto-strategy")).to_contain_text("móvil 5%")
    page.locator('#perp-auto-fields [name="size"]').fill("1")
    page.locator('#perp-auto-fields [name="leverage"]').fill("2")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#perp-signal-create").click()
    expect(page.locator("#perp-signal-status")).to_contain_text("Auto PAPER activado")
    row = page.locator("#perp-signal-sessions tr").filter(has_text="AUTO PAPER")
    session_id = row.locator('[data-signal-action="inspect"]').get_attribute("data-session")
    page.request.post(BASE + "/__test/advance_signal", data={"now": START + 6 * STEP + 10})
    page.wait_for_function("async id => { const r = await fetch('/paper/perp/signals/' + id); const x = await r.json(); return x.actions.length && x.actions[0].status === 'EXECUTED'; }", arg=session_id)
    first = page.request.post(BASE + "/__test/advance_signal_continuous", data={
        "session_id": session_id, "now": START + 7 * STEP + 10})
    assert first.ok and first.json()["poll_result"] == "OBSERVED", first.text()
    second = page.request.post(BASE + "/__test/advance_signal_continuous", data={
        "session_id": session_id, "now": START + 8 * STEP + 10,
        "bar": {"o": "95", "h": "100", "l": "80", "c": "95"}})
    assert second.ok and second.json()["poll_result"] == "OBSERVED", second.text()
    closed = page.request.post(BASE + "/__test/risk_quote", data={
        "session_id": session_id, "now": START + 8 * STEP + 11,
        "mark": "90", "bid": "89", "ask": "91"})
    assert closed.ok and closed.json()["outcome"] == "EXECUTED", closed.text()
    page.locator('[data-signal-action="inspect"][data-session="' + session_id + '"]').click()
    expect(page.locator("#perp-signal-detail")).to_contain_text("Stop móvil · mark")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator('[data-signal-action="inspect"][data-session="' + session_id + '"]').click()
    expect(page.locator("#perp-signal-detail")).to_contain_text("Stop móvil · mark")
    assert not errors, errors
    browser.close()
    print("GRVT PAPER trailing stop: observed mark close, history and restart OK")
