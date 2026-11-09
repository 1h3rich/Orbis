"""Ajustes desbloquea y bloquea LAB cifrado sin persistir la contraseña."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "browser LAB access password"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    section = page.locator("#cfg-privacidad")
    section.get_by_text("LAB activo", exact=True).click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("bloqueado")
    form = page.locator("#cfg-lab-unlock-form")
    expect(form).to_be_visible()
    form.locator("input[name=passphrase]").fill("incorrect LAB password")
    form.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("contraseña o formato")
    expect(form.locator("input[name=passphrase]")).to_have_value("")
    form.locator("input[name=passphrase]").fill(PASSWORD)
    form.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("desbloqueado")
    expect(page.locator("#cfg-lab-lock")).to_be_visible()
    assert PASSWORD not in str(page.evaluate("Object.values(localStorage)"))
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("LAB activo", exact=True).click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("desbloqueado")
    page.locator("#cfg-lab-lock").click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("bloqueado")
    assert not errors, errors
    browser.close()
    print("Ajustes LAB cifrado: error, desbloqueo, recarga y bloqueo verificados")
