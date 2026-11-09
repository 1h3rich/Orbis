"""UI → View controlado → asiento único → saldo cotejado → cierre persistente."""

import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
PASSWORD = "browser booking vault password"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear", exact=True).click()
    page.locator("#real-mexc-execution summary").click()
    sync = page.get_by_role("button", name="Sincronizar orden", exact=True)
    expect(sync).to_be_disabled()
    expect(page.locator(".real-exec-book")).to_be_hidden()
    page.locator("#real-mexc-connect summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("browser-booking-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("browser-booking-secret")
    page.get_by_role("button", name="Consultar MEXC", exact=True).click()
    expect(sync).to_be_enabled()
    with page.expect_response("**/real/execution/mexc/intents/*/synchronize") as first:
        sync.click()
    assert first.value.status == 200, first.value.text()
    assert first.value.json()["stage"] == "SETTLEMENT_REVIEW_REQUIRED"
    expect(page.locator("#real-notice")).to_contain_text("Ejecuciones registradas")
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Asentada · reserva pendiente")
    expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(1)
    page.request.post(BASE + "/__test/lifecycle", data={"quote_adjustment": "0"})
    with page.expect_response("**/real/execution/mexc/intents/*/synchronize") as second:
        sync.click()
    assert second.value.status == 200, second.value.text()
    assert second.value.json()["stage"] == "COMPLETED"
    assert second.value.json()["book_inventory_matched"] is True
    expect(page.locator("#real-notice")).to_contain_text("Orden sincronizada")
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Orden final · reserva cerrada")
    expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(1)
    expect(sync).to_have_count(0)
    page.get_by_role("button", name="Ocultar importes", exact=True).click()
    page.locator("#real-mexc-execution summary").click()
    assert "650.05" not in page.locator("#real-mexc-execution-list").inner_text()
    page.get_by_role("button", name="Bloquear", exact=True).click()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear", exact=True).click()
    page.locator("#real-mexc-execution summary").click()
    expect(page.locator("#real-mexc-execution-list")).to_contain_text("Orden final · reserva cerrada")
    expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(1)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    browser.close()
    print("MEXC: sincronización recuperable, saldo cotejado, asiento único y reapertura OK")
