"""Flujo REAL aislado: bóveda temporal, PAPER SQLite en archivo y precios manuales.

No usar ``ORBIS_DATABASE_URL=sqlite://`` aquí: sus conexiones en varios hilos
no comparten tablas y convierten el recorrido en una prueba intermitente.
"""

import csv
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get('ORBIS_TEST_BASE_URL', 'http://127.0.0.1:8799')
OUT = Path(os.environ.get('ORBIS_TEST_EXECUTION_DIRECTORY', '/tmp'))
assert Path(os.environ['ORBIS_REAL_VAULT_PATH']).resolve().is_relative_to(OUT.resolve())
assert not Path(os.environ['ORBIS_REAL_VAULT_PATH']).exists()

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # El flujo de la bóveda no depende de las cotizaciones públicas externas.
    page.route("**/market/**", lambda route: route.fulfill(status=503, content_type="application/json", body='{}'))
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.locator("#real-passphrase-confirm").fill("browser smoke secret 123")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator("#real-new-account").wait_for()
    page.locator('#real-new-account [name="name"]').fill("Prueba local")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-event-form").wait_for()
    page.locator('#real-event-form [name="quantity"]').fill("0.125")
    page.locator('#real-event-form [name="unit_price"]').fill("100.50")
    page.get_by_role("button", name="Guardar movimiento").click()
    page.get_by_text("0.125", exact=True).first.wait_for()
    page.locator("#real-kind").select_option("TRANSFER")
    page.locator('#real-event-form [name="quantity"]').fill("0.05")
    page.locator('#real-event-form [name="note"]').fill("Entre carteras propias")
    page.get_by_role("button", name="Guardar movimiento").click()
    page.get_by_text("TRANSFER", exact=True).wait_for()
    page.locator("#real-kind").select_option("ADJUSTMENT_IN")
    page.locator('#real-event-form [name="quantity"]').fill("0.01")
    page.locator('#real-event-form [name="unit_price"]').fill("90")
    page.locator('#real-event-form [name="note"]').fill('=2+2,"quoted"')
    page.get_by_role("button", name="Guardar movimiento").click()
    page.get_by_text("ADJUSTMENT_IN", exact=True).wait_for()
    page.locator('.navegacion-principal a[href="#/inicio"]').click()
    page.get_by_role("heading", name="Patrimonio registrado").wait_for()
    expect(page.locator("#home-real")).to_contain_text("Prueba local", timeout=10000)
    page.locator('#home-real a[href="#/real"]').click()
    expect(page.get_by_role("heading", name="Patrimonio", exact=True)).to_be_visible()
    expect(page.locator("#real-lock")).to_be_visible(timeout=10000)
    page.locator("#real-lock").click()
    page.locator("#real-passphrase").wait_for()
    page.locator('.navegacion-principal a[href="#/inicio"]').click()
    expect(page.locator("#home-real")).to_contain_text("Importes reales protegidos")
    expect(page.locator("#home-real")).not_to_contain_text("Prueba local")
    page.locator('.navegacion-principal a[href="#/real"]').click()
    page.locator("#real-passphrase").wait_for()
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.get_by_role("button", name="Desbloquear").click()
    page.get_by_text("0.125", exact=True).first.wait_for()
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_download() as exported:
        page.locator("#real-export-csv").click()
    download = exported.value
    assert download.suggested_filename.startswith("orbis-real-")
    with open(download.path(), encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 3
    assert all(row["environment"] == "REAL" and row["schema_version"] == "2" for row in rows)
    assert next(row for row in rows if row["kind"] == "BUY")["quantity"] == "0.125"
    assert next(row for row in rows if row["kind"] == "TRANSFER")["note"] == "Entre carteras propias"
    assert next(row for row in rows if row["kind"] == "ADJUSTMENT_IN")["note"] == "'=2+2,\"quoted\""
    assert len({row["request_id"] for row in rows}) == 3
    source_account = page.locator("#real-account").input_value()
    page.locator("#real-add-account summary").click()
    page.locator('#real-add-account-form [name="name"]').fill("Destino CSV")
    page.locator('#real-add-account-form button[type="submit"]').click()
    page.get_by_text("Cuenta REAL creada y seleccionada.").wait_for()
    target_account = page.locator("#real-account").input_value()
    assert target_account != source_account
    assert "Destino CSV" in page.locator("#real-account").inner_text()
    assert page.locator('#real-event-form [name="quote_currency"]').input_value() == "EUR"
    page.locator("#real-csv-import summary").click()
    page.locator("#real-csv-file").set_input_files(download.path())
    page.locator("#real-csv-check").click()
    page.get_by_text("3 movimientos · 3 nuevos · 0 ya presentes", exact=False).wait_for()
    page.locator("#real-csv-confirm").fill("IMPORTAR")
    page.locator("#real-csv-commit").click()
    page.get_by_text("3 movimiento(s) importados", exact=False).wait_for()
    expect(page.locator("#real-history tbody tr")).to_have_count(3)
    assert '=2+2,"quoted"' in page.locator("#real-history").inner_text()
    page.locator("#real-add-account summary").click()
    page.locator('#real-add-account-form [name="name"]').fill("Cuenta USDT")
    page.locator('#real-add-account-form [name="base_currency"]').fill("USDT")
    page.locator('#real-add-account-form button[type="submit"]').click()
    page.get_by_text("Cuenta REAL creada y seleccionada.").wait_for()
    assert page.locator('#real-event-form [name="quote_currency"]').input_value() == "USDT"
    expect(page.locator("#real-history")).to_contain_text("Todavía no hay movimientos")
    page.locator("#real-account").select_option(target_account)
    assert page.locator('#real-event-form [name="quote_currency"]').input_value() == "EUR"
    expect(page.locator("#real-history tbody tr")).to_have_count(3)
    page.locator("#real-account").select_option(source_account)
    page.locator("#real-backup").click()
    page.get_by_text("Copia cifrada creada", exact=False).wait_for()
    page.locator("#real-kind").select_option("CONTRIBUTION")
    page.locator('#real-event-form [name="amount"]').fill("500")
    page.get_by_role("button", name="Guardar movimiento").click()
    page.get_by_text("CONTRIBUTION", exact=True).wait_for()
    page.locator("#real-recovery summary").click()
    page.locator("#real-restore-name option[value^='real-']").first.wait_for(state="attached")
    page.locator("#real-restore-name").select_option(index=1)
    page.locator("#real-restore-confirm").fill("RESTAURAR")
    page.screenshot(path=str(OUT / "real-recovery.png"), full_page=True)
    page.get_by_role("button", name="Restaurar y bloquear").click()
    page.locator("#real-passphrase").wait_for()
    page.get_by_text("Copia restaurada", exact=False).wait_for()
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.get_by_role("button", name="Desbloquear").click()
    page.get_by_text("0.125", exact=True).first.wait_for()
    assert page.get_by_text("CONTRIBUTION", exact=True).count() == 0
    assert page.get_by_text("ADJUSTMENT_IN", exact=True).count() == 1
    assert not errors, errors
    (OUT / 'result.json').write_text(json.dumps({'passed': True, 'csv_events': len(rows),
        'transfer_note_preserved': True, 'adjustment_note_preserved': True,
        'backup_restored': True, 'errors': errors}, indent=2))
    print("Browser REAL flow passed: buy, transfer, adjustment, dashboard, lock, backup and restore")
    browser.close()
