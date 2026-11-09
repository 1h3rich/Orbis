"""UI GRVT View contra servidor aislado, sin credenciales ni red externas."""

from playwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/real")
    page.wait_for_load_state("networkidle")
    assert page.locator("#real-auth-form").count() == 1
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.locator("#real-passphrase-confirm").fill("browser smoke secret 123")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator("#real-new-account").wait_for()
    page.locator('#real-new-account [name="name"]').fill("Principal")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-grvt-connect").wait_for()
    page.get_by_text("Sin conexión. Introduce una clave View nueva.").wait_for()
    page.locator("#real-grvt-connect summary").click()
    page.locator('#real-grvt-form [name="api_key"]').fill("browser-grvt-view-key")
    with page.expect_response("**/real/exchanges/grvt/connect") as connection:
        page.get_by_role("button", name="Consultar GRVT").click()
    assert connection.value.status == 200, connection.value.text()
    page.locator("#real-grvt-state").get_by_text("BTC_USDT_Perp").wait_for()
    assert "110.01" in page.locator("#real-grvt-state").inner_text()
    assert "LONG" in page.locator("#real-grvt-state").inner_text()
    assert "OBSERVED_NOT_IMPORTED" in connection.value.text()
    assert page.locator('#real-grvt-form [name="api_key"]').input_value() == ""
    assert "browser-grvt-view-key" not in page.locator("body").inner_text()
    page.locator("#real-grvt-orders summary").click()
    page.locator("#real-grvt-orders-state").get_by_text("BTC_USDT_Perp").wait_for()
    assert "0.3" in page.locator("#real-grvt-orders-state").inner_text()
    assert "No es una conciliación apta para operar" in page.locator("#real-grvt-orders-state").inner_text()
    with page.expect_response("**/real/exchanges/grvt/order?**") as recheck:
        page.locator("#real-grvt-orders-state button[data-grvt-order]").click()
    assert recheck.value.status == 200
    page.locator("#real-grvt-order-detail").get_by_text("FILLED").wait_for()
    assert "0.3 / 0.3" in page.locator("#real-grvt-order-detail").inner_text()
    with page.expect_response("**/real/exchanges/grvt/order-fills/archive") as fills:
        page.get_by_role("button", name="Ver y guardar ejecuciones").click()
    assert fills.value.status == 200
    assert fills.value.json()["archive"]["archived"] is True
    page.locator("#real-grvt-fills-detail").get_by_text("trade-1").wait_for()
    assert "TAMAÑOS COINCIDEN" in page.locator("#real-grvt-fills-detail").inner_text()
    assert "0.12" in page.locator("#real-grvt-fills-detail").inner_text()
    page.get_by_text("Todavía no hay movimientos.").wait_for()
    page.screenshot(path="/tmp/orbis-grvt-readonly-ui.png", full_page=True)
    page.get_by_role("button", name="Ocultar importes").click()
    page.locator("#real-grvt-state").get_by_text("••••").first.wait_for()
    assert "110.01" not in page.locator("#real-grvt-state").inner_text()
    page.locator("#real-grvt-orders summary").click()
    page.locator("#real-grvt-orders-state").get_by_text("BTC_USDT_Perp").wait_for()
    assert "0.3" not in page.locator("#real-grvt-orders-state").inner_text()
    page.locator('#real-grvt-order-form [name="sub_account_id"]').fill("123456")
    page.locator('#real-grvt-order-form [name="client_order_id"]').fill("9223372036854775808")
    page.locator("#real-grvt-order-form").get_by_role("button", name="Comprobar orden").click()
    page.locator("#real-grvt-order-detail").get_by_text("FILLED").wait_for()
    assert "0.3" not in page.locator("#real-grvt-order-detail").inner_text()
    page.get_by_role("button", name="Ver y guardar ejecuciones").click()
    page.locator("#real-grvt-fills-detail").get_by_text("trade-1").wait_for()
    assert "0.12" not in page.locator("#real-grvt-fills-detail").inner_text()
    page.locator("#real-grvt-archive summary").click()
    page.locator("#real-grvt-archive-list").get_by_text("FILLED").wait_for()
    page.locator("#real-grvt-archive-list button[data-grvt-evidence]").click()
    page.locator("#real-grvt-archive-detail").get_by_text("trade-1").wait_for()
    assert "0.12" not in page.locator("#real-grvt-archive-detail").inner_text()
    page.locator("#real-grvt-disconnect").click()
    page.get_by_text("Desconectado. Sesión GRVT retirada de memoria.").wait_for()
    page.get_by_role("button", name="Bloquear").click()
    page.locator("#real-passphrase").wait_for()
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-grvt-archive summary").wait_for()
    page.locator("#real-grvt-archive summary").click()
    page.locator("#real-grvt-archive-list").get_by_text("FILLED").wait_for()
    assert page.locator("#real-grvt-actions").is_hidden()
    assert not errors, errors
    print("Browser GRVT View passed: connect, fills archive, hide, disconnect, lock, reopen")
    browser.close()
