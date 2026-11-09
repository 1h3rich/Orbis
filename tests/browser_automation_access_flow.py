"""Ajustes desbloquea DCA cifrado y las reglas sobreviven a la recarga."""

import os
from pathlib import Path
import subprocess
import sys

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "automation browser test password"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("DCA y automatizaciones", exact=True).click()
    expect(page.locator("#cfg-automation-status")).to_contain_text("bloqueado")
    assert page.request.get(BASE + "/automation/dca/rules").status == 423
    form = page.locator("#cfg-automation-unlock-form")
    expect(form).to_be_visible()
    form.locator("input[name=passphrase]").fill("incorrect automation password")
    form.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#cfg-automation-status")).to_contain_text("contraseña o formato")
    expect(form.locator("input[name=passphrase]")).to_have_value("")
    form.locator("input[name=passphrase]").fill(PASSWORD)
    form.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#cfg-automation-status")).to_contain_text("desbloqueado")
    rules = page.request.get(BASE + "/automation/dca/rules")
    assert rules.status == 200 and rules.json()[0]["name"] == "DCA prueba"
    active_path = os.environ.get("ORBIS_AUTOMATION_PATH")
    if active_path:
        script = Path(__file__).resolve().parents[1] / "scripts" / "restore_automation.py"
        backup = next((Path(active_path).parent / "automation-backups").glob("automation-secure-*.db"))
        blocked = subprocess.run([
            sys.executable, str(script), "--automation-db", active_path,
            "--restore", backup.name, "--confirm", "RESTAURAR DCA", "--passphrase-stdin",
        ], input=PASSWORD + "\n", capture_output=True, text=True)
        assert blocked.returncode == 1 and "sigue usando automatizaciones" in blocked.stderr
    assert PASSWORD not in str(page.evaluate("Object.values(localStorage)"))
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("DCA y automatizaciones", exact=True).click()
    expect(page.locator("#cfg-automation-status")).to_contain_text("desbloqueado")
    page.locator("#cfg-automation-lock").click()
    expect(page.locator("#cfg-automation-status")).to_contain_text("bloqueado")
    assert page.request.get(BASE + "/automation/dca/rules").status == 423
    assert not errors, errors
    browser.close()
    print("Ajustes DCA cifrado: error, desbloqueo, reglas, recarga y bloqueo verificados")
