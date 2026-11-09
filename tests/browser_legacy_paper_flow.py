"""Ajustes: bloquear y desbloquear PAPER manual cifrado, sin guardar clave."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8776"
PASSWORD = "clave-paper-browser-aislada"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    panel = page.locator("#cfg-legacy-status")
    expect(panel).to_contain_text("PAPER cifrado")
    page.locator("#cfg-legacy-status").locator("xpath=ancestor::details").locator("summary").click()
    page.locator("#cfg-legacy-lock").click()
    expect(panel).to_contain_text("bloqueado")
    assert page.request.get(BASE + "/strategies").status == 423
    page.locator("#cfg-legacy-unlock-form [name=passphrase]").fill(PASSWORD)
    page.locator("#cfg-legacy-unlock-form button").click()
    expect(panel).to_contain_text("PAPER cifrado")
    assert page.request.get(BASE + "/strategies").status == 200
    assert PASSWORD not in str(page.evaluate("Object.entries(localStorage)"))
    assert not errors, errors
    print("PAPER manual: bloqueo, rechazo 423, desbloqueo y clave no persistida")
    browser.close()
