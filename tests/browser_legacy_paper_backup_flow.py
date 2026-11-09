"""Ajustes crea una copia SQLCipher sin convertir PAPER activo."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8777"
PASSWORD = "browser legacy backup password"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    status = page.locator("#cfg-legacy-status")
    expect(status).to_contain_text("PAPER sin cifrar")
    status.locator("xpath=ancestor::details").locator("summary").click()
    expect(page.locator("#cfg-legacy-copies")).to_be_visible()
    expect(page.locator("#cfg-legacy-copies")).to_contain_text("1 copia(s) SQLite en claro")
    expect(page.locator("#cfg-legacy-copies")).to_contain_text("orbis.db.bak")
    assert page.request.get(BASE + "/paper/legacy/status").json()["plaintext_sibling_copies"] == ["orbis.db.bak"]
    form = page.locator("#cfg-legacy-backup-form")
    expect(form).to_be_visible()
    form.locator('[name="passphrase"]').fill(PASSWORD)
    form.locator('[name="repeat"]').fill("contraseña distinta")
    form.locator('[name="acknowledge"]').check()
    form.locator('button[type="submit"]').click()
    expect(page.locator("#cfg-legacy-backup-state")).to_contain_text("Las claves no coinciden")
    expect(form.locator('[name="passphrase"]')).to_have_value('')
    form.locator('[name="passphrase"]').fill(PASSWORD)
    form.locator('[name="repeat"]').fill(PASSWORD)
    with page.expect_response("**/paper/legacy/backup/encrypted") as saved:
        form.locator('button[type="submit"]').click()
    result = saved.value
    assert result.status == 200, result.text()
    payload = result.json()
    assert payload["encrypted_backup"] is True
    assert payload["active_database_unchanged"] is True
    backup = Path(payload["path"])
    assert backup.is_file() and backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert (backup.parent.parent / "orbis.db").read_bytes()[:16] == b"SQLite format 3\x00"
    expect(page.locator("#cfg-legacy-backup-state")).to_contain_text("PAPER activo sigue sin cifrar")
    assert PASSWORD not in str(page.evaluate("Object.entries(localStorage)"))
    assert not errors, errors
    browser.close()
    print("PAPER: copia cifrada verificable desde Ajustes; base activa preservada")
