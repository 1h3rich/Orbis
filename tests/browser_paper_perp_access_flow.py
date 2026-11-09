"""Ajustes desbloquea GRVT PAPER cifrado en un servidor local temporal."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    section = page.locator("#cfg-privacidad details").filter(
        has=page.locator("summary", has_text="GRVT PAPER"))
    section.locator("summary").click()
    expect(section.locator("#cfg-perp-status")).to_contain_text("cifrado · bloqueado")
    assert page.request.get(BASE + "/paper/perp/accounts").status == 423
    section.locator('[name="passphrase"]').fill("GRVT paper browser secret 123")
    section.get_by_role("button", name="Desbloquear").click()
    expect(section.locator("#cfg-perp-status")).to_contain_text("min restantes")
    assert page.request.get(BASE + "/paper/perp/accounts").status == 200
    section.get_by_role("button", name="Bloquear GRVT PAPER").click()
    expect(section.locator("#cfg-perp-status")).to_contain_text("cifrado · bloqueado")
    assert page.request.get(BASE + "/paper/perp/accounts").status == 423
    assert not errors, errors
    page.screenshot(path="/tmp/orbis-paper-perp-access-ui.png", full_page=True)
    print("Browser GRVT PAPER access passed: locked, unlock, accounts, relock")
    browser.close()
