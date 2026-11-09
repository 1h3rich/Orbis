"""Wallet sintética + API y SQLCipher reales: permiso → compra → venta, sin fondos."""

import json
import os
import csv
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from uuid import uuid4

from eth_account import Account
from playwright.sync_api import expect, sync_playwright


BOOKING = os.environ.get("ORBIS_TEST_SWAP_BOOKING") == "1"
EXISTING = os.environ.get("ORBIS_TEST_SWAP_BOOKING_EXISTING") == "1"
BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
OUTPUT = Path(os.environ["ORBIS_TEST_SWAP_DIRECTORY"])

from tests.browser_wallet_swap_common import install_provider


with sync_playwright() as playwright:
    wallet = Account.create()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    install_provider(page,wallet)
    errors, swap_posts = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda req: swap_posts.append(req.url)
            if req.method == "POST" and "/wallet-swaps" in req.url else None)
    prompt_value = {"value": ""}
    page.on("dialog", lambda dialog: dialog.accept(prompt_value["value"]) if dialog.type == "prompt" else dialog.accept())
    page.goto(BASE+"/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=str(OUTPUT/"orbis-wallet-swap-baseline.png"))
    password = "isolated browser wallet swap password"
    page.locator("#real-passphrase").fill(password)
    page.locator("#real-passphrase-confirm").fill(password)
    page.get_by_role("button", name="Crear bóveda", exact=True).click()
    page.locator('#real-new-account [name="name"]').fill("Wallet BNB")
    page.get_by_role("button", name="Crear cuenta", exact=True).click()
    if BOOKING:
        for currency, amount in [("USDT", "1000"), ("BNB", "1")]:
            form = page.locator("#real-event-form")
            expect(form.locator('button[type="submit"]')).to_be_enabled()
            form.locator('[name="kind"]').select_option("CONTRIBUTION")
            form.locator('[name="quote_currency"]').fill(currency)
            form.locator('[name="amount"]').fill(amount)
            form.locator('[name="occurred_at"]').fill((datetime.now()-timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))
            with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/events")) as contributed:
                form.locator('button[type="submit"]').click()
            assert contributed.value.status == 200, contributed.value.text()
            expect(form.locator('[name="amount"]')).to_have_value("")
            expect(form.locator('button[type="submit"]')).to_be_enabled()
    page.get_by_role("button", name="Conectar wallet", exact=True).click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    page.get_by_role("button", name="Verificar control", exact=True).click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    if BOOKING:
        page.locator("#real-wallet-links > summary").click()
        page.locator("#real-wallet-link-alias").fill("Wallet BNB")
        page.get_by_role("button", name="Guardar vínculo", exact=True).click()
        expect(page.locator("#real-wallet-proof-state")).to_contain_text("corresponde a esta cuenta REAL")
    panel = page.locator("#real-wallet-swap")
    panel.locator("details > summary").first.click()
    amount = panel.locator('[name="amount"]')
    max_button = panel.get_by_role("button", name="Máximo", exact=True)
    observed = panel.locator('[data-swap-balance]')
    token_picker = panel.locator('[name="token_in"]')
    state = panel.locator('[data-swap-state]')
    page.evaluate("() => {const balances=window.testWallet.balances; balances[Object.keys(balances)[0]]+=1n;}")
    max_button.click()
    expect(amount).to_have_value("1000.000000000000000001")
    expect(observed).to_contain_text("Saldo USDT 1000.000000000000000001 · Gas disponible 1.0 BNB")
    assert not swap_posts and page.evaluate("window.testWallet.sends.length") == 0
    page.evaluate("window.testWallet.changeNextBlockRead=true")
    max_button.click()
    expect(state).to_have_text("El bloque cambió · vuelve a consultar.")
    expect(amount).to_have_value("")
    expect(observed).to_be_hidden()
    max_button.click()
    expect(amount).to_have_value("1000.000000000000000001")
    page.locator("#real-hide").click()
    expect(page.locator("#real-hide")).to_have_text("Mostrar importes")
    page.get_by_role("button", name="Verificar control", exact=True).click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    if not panel.locator("details").first.evaluate("node => node.open"):
        panel.locator("details > summary").first.click()
    expect(max_button).to_be_disabled()
    expect(amount).to_have_value("")
    assert "1000.000000000000000001" not in panel.inner_text()
    page.locator("#real-hide").click()
    expect(page.locator("#real-hide")).to_have_text("Ocultar importes")
    page.get_by_role("button", name="Verificar control", exact=True).click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    if not panel.locator("details").first.evaluate("node => node.open"):
        panel.locator("details > summary").first.click()
    expect(max_button).to_be_enabled()
    page.evaluate("() => {const balances=window.testWallet.balances; balances[Object.keys(balances)[0]]-=1n;}")
    token_picker.select_option("WBNB")
    max_button.click()
    expect(state).to_have_text("Sin saldo de WBNB.")
    expect(amount).to_have_value("")
    token_picker.select_option("USDT")
    page.evaluate("window.testWallet.nativeWei=0n")
    max_button.click()
    expect(state).to_have_text("Sin BNB para gas.")
    expect(observed).to_contain_text("Gas disponible 0.0 BNB")
    page.evaluate("window.testWallet.nativeWei=10n**18n")
    amount.fill("12.345678901234567891")
    page.evaluate("window.testWallet.changeNextBlockRead=true")
    panel.get_by_role("button", name="Cotizar", exact=True).click()
    expect(state).to_have_text("El bloque cambió · vuelve a consultar.")
    expect(panel.locator('[data-swap-prepare]')).to_be_hidden()
    assert not swap_posts and page.evaluate("window.testWallet.sends.length") == 0
    for missing in ("emptyPool", "emptyPoolCode"):
        page.evaluate("field => window.testWallet[field] = true", missing)
        panel.get_by_role("button", name="Cotizar", exact=True).click()
        expect(state).to_have_text("Sin pool directo disponible para este importe.")
        expect(panel.locator('[data-swap-prepare]')).to_be_hidden()
        assert not swap_posts and page.evaluate("window.testWallet.sends.length") == 0
        page.evaluate("field => window.testWallet[field] = false", missing)
    booked_snapshots = []

    def cotizar():
        panel.get_by_role("button", name="Cotizar", exact=True).click()
        try:
            expect(panel.locator("[data-swap-state]")).to_contain_text("Cotización lista", timeout=20000)
        except AssertionError:
            print("DEX error diagnostics", errors, panel.inner_text())
            raise

    def ejecutar(action):
        panel.get_by_role("button", name=action, exact=True).click()
        expect(panel.locator("[data-swap-state]")).to_contain_text("Plan listo")
        if action == "Preparar swap" and page.evaluate("window.testWallet.sends.length") == 1:
            expect(panel.locator("[data-swap-preview]")).to_contain_text("Salida simulada 0.02 WBNB")
            assert panel.locator("[data-swap-preview] small").get_attribute("title").startswith("Pool verificado · 0x")
            page.evaluate("window.testWallet.replacePendingPool = true")
            panel.get_by_role("button", name="Confirmar en wallet", exact=True).click()
            expect(state).to_have_text("El pool cambió · vuelve a cotizar.")
            assert page.evaluate("window.testWallet.sends.length") == 1
            assert len([url for url in swap_posts if url.endswith("/claim")]) == 1
            page.evaluate("window.testWallet.replacePendingPool = false")
            cotizar()
            panel.get_by_role("button", name=action, exact=True).click()
            expect(state).to_contain_text("Plan listo")
        with page.expect_response("**/wallet-swaps/*/hash") as sent:
            panel.get_by_role("button", name="Confirmar en wallet", exact=True).click()
        assert sent.value.status == 200, sent.value.text()
        if not panel.locator("details details").evaluate("element => element.open"):
            panel.get_by_text("Historial DEX", exact=True).click()
        with page.expect_response("**/dex-call-resolutions/*/*/check-original") as checked:
            panel.get_by_role("button", name="Comprobar recibo", exact=True).click()
        assert checked.value.status == 200, checked.value.text()
        assert checked.value.json()["matches_original"] is True
        assert set(checked.value.request.post_data_json) == {"address", "tx_hash"}
        expect(panel.locator("[data-swap-state]")).to_contain_text("Recibo verificado")
        if BOOKING:
            registrar(EXISTING and action == "Preparar swap" and len(booked_snapshots) == 1)

    def import_existing():
        from app.portfolio.csv_import import COLUMNS
        stamp = page.evaluate("parseInt(Object.values(window.testWallet.blocks)[1].timestamp,16)")
        at = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        account = page.locator("#real-account").input_value()
        stream = StringIO(); writer = csv.writer(stream); writer.writerow(COLUMNS)
        for fields in [dict(kind="BUY", asset="WBNB", quote_currency="USDT", quantity="0.02", unit_price="617.28394506172839455", gross_amount="12.345678901234567891"),
                       dict(kind="FEE", quote_currency="BNB", amount="0.0001", gross_amount="0.0001")]:
            row = {"schema_version":"3", "environment":"REAL", "account_id":account, "event_id":str(uuid4()), "request_id":str(uuid4()),
                   "fee":"0", "note":"Anotado previamente", "occurred_at":at, "recorded_at":at, **fields}
            writer.writerow([row.get(key, "") for key in COLUMNS])
        page.locator("#real-csv-import > summary").click()
        page.locator("#real-csv-file").set_input_files({"name":"anotaciones.csv","mimeType":"text/csv","buffer":stream.getvalue().encode()})
        with page.expect_response("**/csv-preview") as reviewed:
            page.locator("#real-csv-check").click()
        assert reviewed.value.status == 200 and reviewed.value.json()["new"] == 2, reviewed.value.text()
        page.locator("#real-csv-confirm").fill("IMPORTAR")
        with page.expect_response("**/csv-import") as imported:
            page.locator("#real-csv-commit").click()
        assert imported.value.status == 200 and imported.value.json()["new"] == 2, imported.value.text()

    def registrar(existing=False):
        if existing:
            import_existing()
        panel.get_by_role("button", name="Revisar registro", exact=True).first.click()
        details = panel.locator("[data-swap-booking]")
        expect(details).to_be_visible()
        expect(details).to_contain_text("Wallet BNB")
        if existing:
            expect(details.get_by_role("button", name="Registrar", exact=True)).to_be_disabled()
            details.get_by_label("Operación existente", exact=True).select_option(index=1)
            details.locator('input[type="checkbox"]').check()
            with page.expect_response("**/booking-preview") as selected:
                details.get_by_role("button", name="Revisar selección", exact=True).click()
            assert selected.value.status == 200 and selected.value.json()["available"], selected.value.text()
            details.screenshot(path=str(OUTPUT/"orbis-wallet-swap-existing-preview.png"))
            page.set_viewport_size({"width":390,"height":844})
            details.screenshot(path=str(OUTPUT/"orbis-wallet-swap-existing-preview-mobile.png"))
            assert details.evaluate("element => element.scrollWidth <= element.clientWidth+1")
            page.set_viewport_size({"width":1440,"height":1000})
        with page.expect_response("**/dex-receipts/CALL_RESOLUTION/*/book") as recorded, page.expect_response("**/snapshot?include_events=true") as refreshed:
            details.get_by_role("button", name="Registrar", exact=True).click()
        assert recorded.value.status == 200 and recorded.value.json()["booked"], recorded.value.text()
        if existing:
            assert recorded.value.json()["events_created"] == 0
        expect(panel.locator("[data-swap-state]")).to_contain_text("Contabilizado")
        expect(panel.locator("[data-swap-history]")).to_contain_text("contabilizado")
        booked_snapshots.append(refreshed.value.json())
        return refreshed.value.json()

    cotizar()
    assert page.evaluate("window.testWallet.sends.length") == 0
    ejecutar("Preparar permiso")
    cotizar()
    ejecutar("Preparar swap")
    assert page.evaluate("window.testWallet.sends.length") == 2
    token_picker.select_option("WBNB")
    max_button.click()
    expect(amount).to_have_value("0.02")
    expect(observed).to_contain_text("Saldo WBNB 0.02")
    cotizar(); ejecutar("Preparar permiso")
    cotizar(); ejecutar("Preparar swap")
    assert page.evaluate("window.testWallet.sends.length") == 4
    expect(panel.locator("[data-swap-history]")).to_contain_text("Confirmada")
    history = panel.locator("[data-swap-history]")
    expect(history).to_contain_text("Recibido 0.02 WBNB · Mínimo 0.0199 WBNB")
    expect(history).to_contain_text("Recibido 13.21 USDT · Mínimo 13.14395 USDT")
    # Respuesta perdida después de emitir: no repetir ni siquiera con otro plan.
    panel.locator('[name="token_in"]').select_option("USDT")
    panel.locator('[name="amount"]').fill("1")
    cotizar()
    panel.get_by_role("button", name="Preparar permiso", exact=True).click()
    expect(panel.locator("[data-swap-state]")).to_contain_text("Plan listo")
    page.evaluate("window.testWallet.mode = 'unknown'")
    panel.get_by_role("button", name="Confirmar en wallet", exact=True).click()
    expect(panel.locator("[data-swap-state]")).to_contain_text("Resultado desconocido")
    assert page.evaluate("window.testWallet.sends.length") == 5
    cotizar()
    panel.get_by_role("button", name="Preparar swap", exact=True).click()
    expect(panel.locator("[data-swap-state]")).to_contain_text("Plan listo")
    panel.get_by_role("button", name="Confirmar en wallet", exact=True).click()
    expect(panel.locator("[data-swap-state]")).to_contain_text("pendiente")
    assert page.evaluate("window.testWallet.sends.length") == 5
    prompt_value["value"] = page.evaluate("Object.keys(window.testWallet.receipts).at(-1)")
    page.evaluate("window.testWallet.mode = 'ok'")
    with page.expect_response("**/dex-call-resolutions/*/*/check-original") as recovered:
        panel.get_by_role("button", name="Comprobar recibo", exact=True).click()
    assert recovered.value.status == 200, recovered.value.text()
    expect(panel.locator("[data-swap-state]")).to_contain_text("Recibo verificado")
    if BOOKING:
        registrar()
    # Rechazo de la wallet: informe explícito, sin reintento ni recibo inventado.
    cotizar()
    panel.get_by_role("button", name="Preparar swap", exact=True).click()
    expect(panel.locator("[data-swap-state]")).to_contain_text("Plan listo")
    page.evaluate("window.testWallet.mode = 'reject'")
    panel.get_by_role("button", name="Confirmar en wallet", exact=True).click()
    expect(panel.locator("[data-swap-state]")).to_contain_text("Rechazo comunicado")
    assert page.evaluate("window.testWallet.sends.length") == 6
    page.evaluate("window.testWallet.mode = 'ok'")
    panel.locator('[name="max_gas"]').fill("0.0000001")
    cotizar()
    panel.get_by_role("button", name="Preparar swap", exact=True).click()
    expect(panel.locator("[data-swap-state]")).to_contain_text("Gas por encima de tu límite")
    assert page.evaluate("window.testWallet.sends.length") == 6
    panel.screenshot(path=str(OUTPUT/"orbis-wallet-swap-roundtrip.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    panel.screenshot(path=str(OUTPUT/"orbis-wallet-swap-roundtrip-mobile.png"))
    assert wallet.address.lower() not in page.evaluate("JSON.stringify(localStorage)").lower()
    assert not errors, errors
    if BOOKING:
        expect(page.locator("#real-history")).to_contain_text("Comisión")
        assert len(booked_snapshots) == 5
        last = booked_snapshots[-1]
        assert Decimal(last["cash"]["USDT"]) == Decimal("1000.864321098765432109")
        assert Decimal(last["cash"]["BNB"]) == Decimal("0.9995")
        assert last["contributions"] == {"USDT": "1000", "BNB": "1"} and not last["withdrawals"]
        assert all(Decimal(position["quantity"]) == 0 for position in last["positions"] if position["asset"] == "WBNB")
        with page.expect_download() as exported:
            page.locator("#real-export-csv").click()
        with open(exported.value.path(), encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        assert len(rows) == 9 and all(row["schema_version"] == "3" for row in rows)
        fees = [row for row in rows if row["kind"] == "FEE"]
        assert len(fees) == 5 and sum(Decimal(row["amount"]) for row in fees) == Decimal("0.0005")
        print("PASS: registro explícito de compra/venta y gas, API/SQLCipher reales, importes y posiciones actualizados; cero fondos.")
    page.get_by_role("button", name="Bloquear", exact=True).click()
    page.locator("#real-passphrase").fill(password)
    page.get_by_role("button", name="Desbloquear", exact=True).click()
    panel.locator("details > summary").first.click()
    expect(panel.get_by_role("button", name="Cotizar", exact=True)).to_be_disabled()
    panel.get_by_text("Historial DEX", exact=True).click()
    expect(history).to_contain_text("Recibido 13.21 USDT · Mínimo 13.14395 USDT")
    page.locator("#real-hide").click()
    expect(page.locator("#real-hide")).to_have_text("Mostrar importes")
    if not panel.locator("details").first.evaluate("node => node.open"):
        panel.locator("details > summary").first.click()
    if not panel.locator("details details").evaluate("node => node.open"):
        panel.get_by_text("Historial DEX", exact=True).click()
    expect(history).to_contain_text("Recibido •••• USDT · Mínimo •••• USDT")
    assert "12.345678901234567891" not in page.locator("#real-wallet-swap").inner_text()
    assert "Recibido 13.21" not in history.inner_text()
    assert not errors, errors
    (OUTPUT / "swap-review-result.json").write_text(json.dumps({
        "passed":True, "unverified_pools_blocked":True, "pool_change_blocked_before_claim":True,
        "simulation_visible":True, "receipt_offline":True, "amounts_masked":True,
        "booked_actions":len(booked_snapshots), "wallet_requests":page.evaluate("window.testWallet.sends.length"),
    }))
    print("PASS: compra/venta, resultado incierto sin reenvío y recuperación, rechazo, límite de gas y ocultación; API/SQLCipher reales, wallet sintética, cero fondos.")
    browser.close()
