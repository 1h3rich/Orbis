"""Copia PAPER SQLCipher real desde Ajustes, en un servidor aislado."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from app.database.legacy_backup import verify_legacy_encrypted_backup


BASE = "http://127.0.0.1:8778"
BACKUP_KEY = "browser backup separate passphrase"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-legacy-status").locator("xpath=ancestor::details").locator("summary").click()
    expect(page.locator("#cfg-legacy-status")).to_contain_text("PAPER cifrado")
    form = page.locator("#cfg-legacy-backup-form")
    expect(form).to_be_visible()
    form.locator('[name="passphrase"]').fill(BACKUP_KEY)
    form.locator('[name="repeat"]').fill(BACKUP_KEY)
    form.locator('[name="acknowledge"]').check()
    with page.expect_response("**/paper/legacy/backup/encrypted") as saved:
        form.locator('button[type="submit"]').click()
    result = saved.value
    assert result.status == 200, result.text()
    data = result.json()
    assert data["active_database_unchanged"] is True
    assert data["active_database_encrypted"] is True
    backup = Path(data["path"])
    assert backup.is_file() and backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert verify_legacy_encrypted_backup(backup, BACKUP_KEY)["fingerprint"] == data["fingerprint"]
    assert (backup.parent.parent / "orbis.db").read_bytes()[:16] != b"SQLite format 3\x00"
    expect(page.locator("#cfg-legacy-backup-state")).to_contain_text("PAPER activo permanece cifrado")
    assert BACKUP_KEY not in page.evaluate("JSON.stringify(localStorage)")

    locked = page.request.post(BASE + "/paper/legacy/lock")
    assert locked.status == 200
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-legacy-status").locator("xpath=ancestor::details").locator("summary").click()
    expect(page.locator("#cfg-legacy-backup-form")).to_be_hidden()
    denied = page.request.post(BASE + "/paper/legacy/backup/encrypted", data={
        "passphrase": BACKUP_KEY, "confirmation": "COPIA PAPER"})
    assert denied.status == 423
    assert not errors, errors
    browser.close()
    print("PAPER cifrado: copia reabierta y verificada; bloqueo impide nueva copia")
