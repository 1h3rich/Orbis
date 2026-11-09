"""Alta de LAB nuevo cifrado desde Ajustes en un perfil aislado."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "orbis lab browser creation password"
path = Path(os.environ["ORBIS_LAB_PATH"])
assert not path.exists()

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("LAB activo", exact=True).click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("no está inicializado")
    assert not path.exists()

    form = page.locator("#cfg-lab-create-form")
    expect(form).to_be_visible()
    form.locator("input[name=passphrase]").fill(PASSWORD)
    form.get_by_role("button", name="Crear LAB cifrado").click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("desbloqueado")
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert PASSWORD not in str(page.evaluate("Object.values(localStorage)"))

    page.locator("#cfg-lab-lock").click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("bloqueado")
    assert page.request.get(BASE + "/lab/datasets").status == 423
    unlock = page.locator("#cfg-lab-unlock-form")
    unlock.locator("input[name=passphrase]").fill(PASSWORD)
    unlock.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("desbloqueado")

    candles = [{"t": 1735689600 + i * 86400, "o": "100", "h": "101",
                "l": "99", "c": "100", "v": "10"} for i in range(3)]
    imported = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1d", "source": "browser-fixture",
        "candles": candles})
    assert imported.ok, imported.text()
    dataset_id = imported.json()["id"]
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    page.reload()
    page.wait_for_load_state("networkidle")
    assert any(row["id"] == dataset_id for row in page.request.get(BASE + "/lab/datasets").json())
    assert not errors, errors
    browser.close()

print("LAB nuevo: alta cifrada, bloqueo, histórico persistente y clave no persistida verificados")
