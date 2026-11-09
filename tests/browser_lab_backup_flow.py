"""Configuración crea una copia LAB cifrada y recuperable offline."""

import os
from pathlib import Path
import subprocess
import sys

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "browser LAB backup password"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    candles = [{"t": 1735689600 + i * 3600, "o": str(100 + i),
                "h": str(102 + i), "l": str(99 + i), "c": str(101 + i),
                "v": "10"} for i in range(12)]
    response = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "CSV local", "candles": candles})
    assert response.status == 200, response.text()

    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    section = page.locator("#cfg-privacidad")
    expect(section).to_contain_text("Copias LAB")
    section.get_by_text("Copias LAB", exact=True).click()
    expect(page.locator("#cfg-lab-backups")).to_contain_text("Aún no hay copias LAB")
    page.locator("#cfg-lab-backup-form input[name=passphrase]").fill(PASSWORD)
    page.locator("#cfg-lab-backup").click()
    expect(page.locator("#cfg-lab-backups")).to_contain_text("Copia cifrada verificada")
    expect(page.locator("#cfg-lab-backup-form input[name=passphrase]")).to_have_value("")
    expect(section).to_contain_text("El estado de LAB activo aparece arriba")
    copies = page.request.get(BASE + "/lab/backups")
    assert copies.status == 200 and len(copies.json()) == 1
    assert copies.json()[0]["encrypted"] is True
    lab_path = os.environ.get("ORBIS_LAB_PATH")
    if lab_path:
        script = Path(__file__).resolve().parents[1] / "scripts" / "restore_lab.py"
        blocked = subprocess.run([
            sys.executable, str(script), "--lab-db", lab_path,
            "--restore", copies.json()[0]["name"], "--confirm", "RESTAURAR",
            "--passphrase-stdin",
        ], input=PASSWORD + "\n", capture_output=True, text=True)
        assert blocked.returncode == 1 and "sigue usando LAB" in blocked.stderr, (
            blocked.returncode, blocked.stderr)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("Copias LAB", exact=True).click()
    expect(page.locator("#cfg-lab-backups")).to_contain_text(copies.json()[0]["name"])
    assert not errors, errors
    browser.close()
    print("Configuración: copia LAB cifrada visible tras recarga; restauración bloqueada en vivo")
