"""Parada local visible: no confunde reserva retenida con cancelación MEXC."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "browser booking vault password"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-mexc-execution summary").wait_for()
    page.locator("#real-mexc-execution summary").click()
    stop = page.locator("#real-mexc-execution-list .real-exec-stop")
    stop.locator("summary").click()
    expect(stop).to_contain_text("No cancela órdenes en MEXC")
    stop.locator('input[name="confirmation"]').fill("DETENER")
    with page.expect_response("**/real/execution/mexc/intents/*/stop-account") as stopped:
        stop.get_by_role("button", name="Detener").click()
    assert stopped.value.status == 200, stopped.value.text()
    result = stopped.value.json()
    assert result["stopped"] is True
    assert result["unresolved_sent_or_unknown"] == 1
    assert result["exchange_orders_cancelled"] is False
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Parada local activa")
    assert page.locator("#real-mexc-execution-list .real-exec-stop:not(.real-exec-supervise)").count() == 0
    observe = page.locator("#real-mexc-execution-list .real-exec-observe")
    expect(observe.get_by_role("button", name="Consultar MEXC")).to_be_visible()
    with page.expect_response("**/real/execution/mexc/intents/*/observe-stopped") as checked:
        observe.get_by_role("button", name="Consultar MEXC").click()
    assert checked.value.status == 409
    expect(observe).to_contain_text("Conecta la clave MEXC View")
    page.locator("#real-mexc-connect summary").click()
    page.locator('#real-mexc-form input[name="api_key"]').fill("browser-booking-key")
    page.locator('#real-mexc-form input[name="secret_key"]').fill("browser-booking-secret")
    with page.expect_response("**/real/exchanges/mexc/connect") as connected:
        page.locator("#real-mexc-form").get_by_role("button", name="Consultar MEXC").click()
    assert connected.value.status == 200, connected.value.text()
    with page.expect_response("**/real/execution/mexc/intents/*/observe-stopped") as archived:
        observe.get_by_role("button", name="Consultar MEXC").click()
    assert archived.value.status == 200, archived.value.text()
    result = archived.value.json()
    assert result["status"] == "FINAL_ARCHIVED_NEEDS_SETTLEMENT"
    assert result["exchange_orders_cancelled"] is False
    assert result["reservation_closed"] is False
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Parada local activa")
    assert not errors, errors
    browser.close()
