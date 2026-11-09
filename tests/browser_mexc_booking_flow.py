"""Recorrido real de UI→API→bóveda cifrada con orden externa archivada de prueba."""

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
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Finalizada · pendiente de asiento")
    expect(page.locator(".real-exec-audit button")).to_be_disabled()
    expect(page.locator(".real-exec-transfer-audit button")).to_be_disabled()
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("-650.05")
    assert page.locator("#real-history").get_by_text("BUY", exact=True).count() == 0
    page.locator('.real-exec-book input[name="confirmation"]').fill("IMPORTAR")
    with page.expect_response("**/real/execution/mexc/intents/*/book") as booking:
        page.get_by_role("button", name="Importar fills").click()
    assert booking.value.status == 200, booking.value.text()
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Asentada · reserva pendiente")
    expect(page.locator("#real-history")).to_contain_text("MEXC Spot · fill verificado")
    expect(page.locator("#real-positions")).to_contain_text("0.01")
    assert page.locator("#real-mexc-execution-list .real-exec-book").count() == 0
    page.locator(".real-exec-manual summary").click()
    page.locator('.real-exec-close input[name="confirmation"]').fill("CERRAR")
    page.get_by_role("button", name="Reconfirmar y cerrar reserva").click()
    expect(page.locator(".real-exec-close .real-exec-result")).to_contain_text("Conecta la clave MEXC View")
    page.locator("#real-mexc-connect summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("browser-booking-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("browser-booking-secret")
    page.get_by_role("button", name="Consultar MEXC").click()
    expect(page.locator("#real-mexc-state")).to_contain_text("0.01")
    expect(page.locator(".real-exec-audit button")).to_be_enabled()
    expect(page.locator(".real-exec-transfer-audit button")).to_be_enabled()
    with page.expect_response("**/real/execution/mexc/policies/*/trade-window-audit") as audit:
        page.get_by_role("button", name="Auditar fills").click()
    assert audit.value.status == 200, audit.value.text()
    expect(page.locator(".real-exec-audit .real-exec-result")).to_contain_text("Ventana consistente")
    expect(page.locator(".real-exec-audit .real-exec-result")).to_contain_text("No autoriza operar")
    with page.expect_response("**/real/execution/mexc/policies/*/transfer-window-audit") as transfer_audit:
        page.get_by_role("button", name="Auditar traslados").click()
    assert transfer_audit.value.status == 200, transfer_audit.value.text()
    expect(page.locator(".real-exec-transfer-audit .real-exec-result")).to_contain_text("Sin traslados en el rango")
    expect(page.locator(".real-exec-transfer-audit .real-exec-result")).to_contain_text("No autoriza operar")
    with page.expect_response("**/real/execution/mexc/intents/*/close-reservation") as closure:
        page.get_by_role("button", name="Reconfirmar y cerrar reserva").click()
    assert closure.value.status == 200, closure.value.text()
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Orden final · reserva cerrada")
    page.locator("#real-mexc-disconnect").click()
    expect(page.locator(".real-exec-audit button")).to_be_disabled()
    expect(page.locator(".real-exec-audit .real-exec-result")).to_be_hidden()
    expect(page.locator(".real-exec-transfer-audit button")).to_be_disabled()
    expect(page.locator(".real-exec-transfer-audit .real-exec-result")).to_be_hidden()
    page.get_by_role("button", name="Ocultar importes").click()
    page.locator("#real-mexc-execution summary").click()
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Orden final · reserva cerrada")
    assert "650.05" not in page.locator("#real-mexc-execution-list").inner_text()
    page.get_by_role("button", name="Bloquear").click()
    page.locator("#real-passphrase").wait_for()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-mexc-execution summary").wait_for()
    page.locator("#real-mexc-execution summary").click()
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Orden final · reserva cerrada")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    print("MEXC REAL booking browser flow passed: confirm, ledger, hidden amounts, reopen")
    browser.close()
