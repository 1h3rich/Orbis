"""Alta cifrada de Spot PAPER desde Ajustes en un perfil aislado."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "orbis spot paper browser password"
path = Path(os.environ["ORBIS_SPOT_STRATEGY_PATH"])
assert not path.exists()

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("Spot PAPER", exact=True).click()
    expect(page.locator("#cfg-spot-status")).to_contain_text("no está inicializado")
    assert not path.exists()

    form = page.locator("#cfg-spot-create-form")
    expect(form).to_be_visible()
    form.locator("input[name=passphrase]").fill(PASSWORD)
    form.get_by_role("button", name="Crear Spot PAPER cifrado").click()
    expect(page.locator("#cfg-spot-status")).to_contain_text("Spot PAPER cifrado ·")
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert PASSWORD not in str(page.evaluate("Object.values(localStorage)"))
    page.locator("#cfg-spot-lock").click()
    expect(page.locator("#cfg-spot-status")).to_contain_text("bloqueado")
    assert page.request.get(BASE + "/paper/spot/strategies").status == 423
    unlock = page.locator("#cfg-spot-unlock-form")
    unlock.locator("input[name=passphrase]").fill(PASSWORD)
    unlock.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#cfg-spot-status")).to_contain_text("Spot PAPER cifrado ·")
    assert page.request.get(BASE + "/paper/spot/strategies").json() == []
    page.reload()
    page.wait_for_load_state("networkidle")
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert not errors, errors
    browser.close()

print("Spot PAPER nuevo: alta cifrada, bloqueo, recarga y clave no persistida verificados")
