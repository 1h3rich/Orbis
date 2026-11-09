"""Alta cifrada de GRVT PAPER desde Ajustes en un perfil aislado."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "orbis grvt paper browser password"
path = Path(os.environ["ORBIS_PAPER_PERP_PATH"])
assert not path.exists()

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("GRVT PAPER", exact=True).click()
    expect(page.locator("#cfg-perp-status")).to_contain_text("no está inicializado")
    assert not path.exists()

    form = page.locator("#cfg-perp-create-form")
    expect(form).to_be_visible()
    form.locator("input[name=passphrase]").fill(PASSWORD)
    form.get_by_role("button", name="Crear GRVT PAPER cifrado").click()
    expect(page.locator("#cfg-perp-status")).to_contain_text("GRVT PAPER cifrado ·")
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert PASSWORD not in str(page.evaluate("Object.values(localStorage)"))

    created = page.request.post(BASE + "/paper/perp/accounts", data={
        "name": "Cuenta cifrada", "initial_balance": "1000",
        "max_position_notional": "500", "max_total_notional": "800",
        "max_leverage": "5", "max_positions": 2,
        "fee_rate": "0.001", "slippage_bps": "0"})
    assert created.status == 201, created.text()
    account_id = created.json()["id"]
    page.locator("#cfg-perp-lock").click()
    expect(page.locator("#cfg-perp-status")).to_contain_text("bloqueado")
    assert page.request.get(BASE + "/paper/perp/accounts").status == 423
    unlock = page.locator("#cfg-perp-unlock-form")
    unlock.locator("input[name=passphrase]").fill(PASSWORD)
    unlock.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#cfg-perp-status")).to_contain_text("GRVT PAPER cifrado ·")
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.request.get(BASE + "/paper/perp/accounts/" + account_id).json()["id"] == account_id
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert not errors, errors
    browser.close()

print("GRVT PAPER nuevo: alta cifrada, cuenta persistente, bloqueo y clave no persistida verificados")
