"""Verificación Trade GET-only en UI, con credenciales MEXC ficticias."""

from playwright.sync_api import expect, sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser trade verification password")
    page.locator("#real-passphrase-confirm").fill("browser trade verification password")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator('#real-new-account [name="name"]').fill("Principal")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-mexc-trade-verify > summary").click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("Conecta primero MEXC View")
    expect(page.locator("#real-mexc-trade-form")).to_be_hidden()
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("browser-test-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("browser-test-secret")
    page.get_by_role("button", name="Consultar MEXC").click()
    expect(page.locator("#real-mexc-trade-form")).to_be_visible()
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("browser-other-trade-key")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("browser-trade-secret")
    page.get_by_role("button", name="Verificar Trade").click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("cuentas distintas")
    assert page.locator('#real-mexc-trade-form [name="api_key"]').input_value() == ""
    assert page.locator('#real-mexc-trade-form [name="secret_key"]').input_value() == ""
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("browser-trade-key")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("browser-trade-secret")
    page.get_by_role("button", name="Verificar Trade").click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("autorización de operativa separada")
    expect(page.locator("#real-mexc-trade-form")).to_be_hidden()
    expect(page.locator("#real-mexc-trade-disconnect")).to_be_visible()
    assert page.locator('#real-mexc-trade-form [name="api_key"]').input_value() == ""
    assert page.locator('#real-mexc-trade-form [name="secret_key"]').input_value() == ""
    assert "browser-trade-secret" not in page.locator("#real-mexc-trade-state").inner_text()
    page.get_by_role("button", name="Retirar clave Trade").click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("Sin clave Trade verificada")
    expect(page.locator("#real-mexc-trade-form")).to_be_visible()
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("browser-trade-key")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("browser-trade-secret")
    page.get_by_role("button", name="Verificar Trade").click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("autorización de operativa separada")
    page.get_by_role("button", name="Bloquear").click()
    page.locator("#real-passphrase").fill("browser trade verification password")
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-mexc-trade-verify > summary").click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("Conecta primero MEXC View")
    assert not errors, errors
    browser.close()
