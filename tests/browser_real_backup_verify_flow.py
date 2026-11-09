"""Comprobación visual aislada de copia REAL, sin modificar el libro activo."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("isolated backup browser password")
    page.locator("#real-passphrase-confirm").fill("isolated backup browser password")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator("#real-new-account").wait_for()
    page.locator('#real-new-account [name="name"]').fill("Principal")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-account").wait_for()
    account_id = page.locator("#real-account").input_value()
    page.locator("#real-backup").click()
    page.get_by_text("Copia cifrada creada", exact=False).wait_for()
    page.locator("#real-recovery summary").click()
    page.locator("#real-restore-name option[value^='real-']").first.wait_for(state="attached")
    expect(page.locator("#real-verify-backup")).to_be_disabled()
    page.locator("#real-restore-name").select_option(index=1)
    expect(page.locator("#real-verify-backup")).to_be_enabled()
    page.locator("#real-verify-backup").click()
    expect(page.locator("#real-backup-check")).to_contain_text("Copia comprobada · 1 cuenta · 0 movimientos")
    expect(page.locator("#real-backup-check")).to_contain_text("Se verificará de nuevo al restaurar")
    expect(page.locator("#real-account")).to_have_value(account_id)
    page.locator("#real-restore-name").select_option("")
    expect(page.locator("#real-verify-backup")).to_be_disabled()
    expect(page.locator("#real-backup-check")).to_be_hidden()
    assert not errors, errors
    browser.close()
