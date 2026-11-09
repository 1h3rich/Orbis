"""Ajustes ofrece copia PAPER cifrada sólo tras desbloqueo."""

import json
import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000")
BACKUP_KEY = "clave-de-copia-browser-separada"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    requests = []
    state = {"unlocked": False, "format": "encrypted_or_invalid"}
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_status(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "format": state["format"], "unlocked": state["unlocked"],
            "remaining_seconds": 900 if state["unlocked"] else 0,
            "plaintext_sibling_copies": [], "unverified_sibling_copies": [],
            "sibling_scan_complete": True,
        }))

    def serve_backup(route):
        requests.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "encrypted_backup": True, "active_database_unchanged": True,
            "active_database_encrypted": True,
            "path": "/ruta/temporal/copia-cifrada.db", "tables": {},
            "user_version": 1, "fingerprint": "fixture",
        }))

    page.route("**/paper/legacy/status", serve_status)
    page.route("**/paper/legacy/backup/encrypted", serve_backup)
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-legacy-status").locator("xpath=ancestor::details").locator("summary").click()
    expect(page.locator("#cfg-legacy-status")).to_contain_text("bloqueado")
    expect(page.locator("#cfg-legacy-backup-form")).to_be_hidden()
    assert not requests

    state["unlocked"] = True
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-legacy-status").locator("xpath=ancestor::details").locator("summary").click()
    form = page.locator("#cfg-legacy-backup-form")
    expect(form).to_be_visible()
    expect(page.locator("#cfg-legacy-backup-ack-text")).to_contain_text("permanece cifrado")
    form.locator('[name="passphrase"]').fill(BACKUP_KEY)
    form.locator('[name="repeat"]').fill(BACKUP_KEY)
    form.locator('[name="acknowledge"]').check()
    form.locator('button[type="submit"]').click()
    expect(page.locator("#cfg-legacy-backup-state")).to_contain_text("PAPER activo permanece cifrado")
    assert requests == [{"passphrase": BACKUP_KEY, "confirmation": "COPIA PAPER"}]
    assert BACKUP_KEY not in page.evaluate("JSON.stringify(localStorage)")

    state["format"] = "plain"
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-legacy-status").locator("xpath=ancestor::details").locator("summary").click()
    expect(page.locator("#cfg-legacy-status")).to_contain_text("PAPER sin cifrar")
    guide = page.locator("#cfg-legacy-migration")
    expect(guide).to_be_visible()
    expect(guide).to_contain_text("cierra ORBIS")
    expect(guide).to_contain_text("scripts.encrypt_legacy_paper")
    expect(page.locator("#cfg-legacy-backup-ack-text")).to_contain_text("sigue sin cifrar")
    assert len(requests) == 1
    assert not errors, errors
    browser.close()
    print("Ajustes: PAPER cifrado bloqueado/abierto y copia diferenciada")
