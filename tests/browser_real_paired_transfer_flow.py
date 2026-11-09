"""Cartera REAL: traslado manual pareado sin llamada a wallet o exchange."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("paired transfer browser secret")
    page.locator("#real-passphrase-confirm").fill("paired transfer browser secret")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator('#real-new-account [name="name"]').fill("Wallet")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-event-form").wait_for()
    wallet_id = page.locator("#real-account").input_value()
    page.locator('#real-event-form [name="quantity"]').fill("2")
    page.locator('#real-event-form [name="unit_price"]').fill("100")
    page.get_by_role("button", name="Guardar movimiento").click()
    expect(page.locator("#real-positions")).to_contain_text("2")
    page.locator("#real-kind").select_option("CONTRIBUTION")
    page.locator('#real-event-form [name="amount"]').fill("300")
    page.get_by_role("button", name="Guardar movimiento").click()
    expect(page.locator("#real-history")).to_contain_text("CONTRIBUTION")

    page.locator("#real-add-account summary").click()
    page.locator('#real-add-account-form [name="name"]').fill("MEXC")
    page.locator('#real-add-account-form button[type="submit"]').click()
    page.get_by_text("Cuenta REAL creada y seleccionada.").wait_for()
    mexc_id = page.locator("#real-account").input_value()
    assert mexc_id != wallet_id
    page.locator("#real-account").select_option(wallet_id)
    page.locator("#real-paired-transfer summary").click()
    form = page.locator("#real-paired-transfer-form")
    form.locator('[name="target_account_id"]').select_option(mexc_id)
    form.locator('[name="quantity"]').fill("0.5")
    form.locator('[name="fee"]').fill("0.01")
    form.locator('[name="note"]').fill("Movimiento externo ya realizado")
    page.once("dialog", lambda dialog: dialog.dismiss())
    form.get_by_role("button", name="Registrar traslado").click()
    expect(form.get_by_role("button", name="Registrar traslado")).to_be_enabled()
    expect(page.locator("#real-history")).not_to_contain_text("TRANSFER_OUT")
    def confirm_preview(dialog):
        assert "0.5 BTC hacia MEXC" in dialog.message
        assert "recibe: 0.49 BTC" in dialog.message
        assert "Coste trasladado: 50" in dialog.message
        assert "Sólo libro local" in dialog.message
        dialog.accept()
    page.once("dialog", confirm_preview)
    form.get_by_role("button", name="Registrar traslado").click()
    expect(page.locator("#real-notice")).to_contain_text("Ningún fondo se movió desde Orbis")
    expect(page.locator("#real-history")).to_contain_text("TRANSFER_OUT")
    expect(page.locator("#real-positions")).to_contain_text("1.5")
    form.locator('[name="mode"]').select_option("cash")
    expect(form.locator('[name="asset"]')).to_be_hidden()
    form.locator('[name="amount"]').fill("50")
    def confirm_cash(dialog):
        assert "50 EUR hacia MEXC" in dialog.message
        assert "Disponible en origen antes: 100 EUR" in dialog.message
        dialog.accept()
    page.once("dialog", confirm_cash)
    form.get_by_role("button", name="Registrar traslado").click()
    expect(page.locator("#real-history")).to_contain_text("CASH_TRANSFER_OUT")
    page.locator("#real-account").select_option(mexc_id)
    expect(page.locator("#real-history")).to_contain_text("TRANSFER_IN")
    expect(page.locator("#real-history")).to_contain_text("CASH_TRANSFER_IN")
    expect(page.locator("#real-positions")).to_contain_text("0.49")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("paired transfer browser secret")
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-account").select_option(mexc_id)
    expect(page.locator("#real-history")).to_contain_text("TRANSFER_IN")
    expect(page.locator("#real-history")).to_contain_text("CASH_TRANSFER_IN")
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    snapshot_id = str(uuid4())
    previous_id = "11111111-1111-4111-8111-111111111111"
    page.route("**/real/exchanges/mexc/transfers/archive", lambda route: route.fulfill(json=[{
        "id": snapshot_id, "account_ref": "a" * 64,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "start_ms": stamp - 86400000, "end_ms": stamp + 86400000,
        "deposit_count": 2, "withdrawal_count": 0,
    }, {
        "id": previous_id, "account_ref": "a" * 64,
        "observed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "start_ms": stamp - 86400000, "end_ms": stamp + 86400000,
        "deposit_count": 1, "withdrawal_count": 0,
    }]))
    page.route("**/real/exchanges/mexc/transfers/archive/*/compare/*", lambda route: route.fulfill(json={
        "conclusion": "OBSERVATIONAL_NOT_RECONCILED",
        "counts": {"unchanged": 0, "status_changed": 0, "finality_regression": 1,
                   "fields_changed": 0, "not_returned": 0, "newly_returned": 0,
                   "ambiguous_reference": 0},
        "unlinked_before": 1, "unlinked_after": 0, "truncated": False,
        "changes": [{"direction": "deposit", "source_ref": "a" * 64,
                     "outcome": "finality_regression", "before": {"status": 5},
                     "after": {"status": 4}}],
    }))
    page.route(f"**/real/exchanges/mexc/transfers/archive/{snapshot_id}", lambda route: route.fulfill(json={
        "id": snapshot_id, "observed_at": datetime.now(timezone.utc).isoformat(),
        "start_ms": stamp - 86400000, "end_ms": stamp + 86400000, "requests": 2,
        "deposits": [{"direction": "deposit", "asset": "EUR", "network": None,
                      "amount": "50", "fee": "0", "status": 5, "time_ms": stamp,
                      "source_ref": "a" * 64},
                     {"direction": "deposit", "asset": "BTC", "network": "BTC",
                      "amount": "0.7", "fee": "0", "status": 5, "time_ms": stamp}],
        "withdrawals": [],
    }))
    page.route("**/real/accounts/*/mexc-transfer-candidates/*", lambda route: route.fulfill(json={
        "conclusion": "CANDIDATES_ONLY_NOT_RECONCILED",
        "account_reference": "UNLINKED_ACCOUNT_ASSUMED", "rows": [{
            "direction": "deposit", "asset": "EUR", "amount": "50",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "result": "POSSIBLE_MATCH", "candidates": [{"row_index": 0}],
        }, {
            "direction": "withdrawal", "asset": "EUR", "amount": "50",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "result": "NON_EXTERNAL_WITHDRAWAL_ONLY", "candidates": [],
        }], "observed_rows": [{
            "direction": "deposit", "asset": "EUR", "amount": "50",
            "observed_at": datetime.now(timezone.utc).isoformat(), "status_code": 5,
            "result": "POSSIBLE_MATCH",
        }, {
            "direction": "deposit", "asset": "BTC", "amount": "0.7",
            "observed_at": datetime.now(timezone.utc).isoformat(), "status_code": 5,
            "result": "NO_PAIRED_LOCAL_CANDIDATE",
        }, {
            "direction": "withdrawal", "asset": "EUR", "amount": "50",
            "observed_at": datetime.now(timezone.utc).isoformat(), "status_code": 7,
            "transfer_type": 1, "result": "INTERNAL_MEXC_NOT_WALLET",
        }],
    }))
    page.locator("#real-mexc-transfers-archive summary").click()
    page.locator("#real-mexc-transfers-list button[data-id]").first.click()
    expect(page.locator("#real-mexc-transfers-detail")).to_contain_text("aaaaaaaaaa…")
    page.locator("#real-mexc-transfer-diff").click()
    expect(page.locator("#real-mexc-transfer-diff-result")).to_contain_text("Final → no final")
    expect(page.locator("#real-mexc-transfer-diff-result")).to_contain_text("sin conciliación")
    page.locator("#real-mexc-transfer-compare").click()
    expect(page.locator("#real-mexc-transfer-candidates")).to_contain_text("Posible")
    expect(page.locator("#real-mexc-transfer-candidates")).to_contain_text("sin conciliación")
    expect(page.locator("#real-mexc-transfer-candidates")).to_contain_text("Sin traslado pareado candidato")
    expect(page.locator("#real-mexc-transfer-candidates")).to_contain_text("Interno MEXC · no es retorno a wallet")
    expect(page.locator("#real-mexc-transfer-candidates")).to_contain_text("Retiro interno o ruta desconocida")
    page.unroute("**/real/accounts/*/mexc-transfer-candidates/*")
    page.route("**/real/accounts/*/mexc-transfer-candidates/*", lambda route: route.fulfill(json={
        "conclusion": "ACCOUNT_REFERENCE_CONFLICT_NO_COMPARISON",
        "account_reference": "CONFLICT", "rows": [],
    }))
    page.locator("#real-mexc-transfer-compare").click()
    expect(page.locator("#real-mexc-transfer-candidates")).to_contain_text("No se compararon movimientos")
    def reject_unpaired_csv(dialog):
        assert "NO se puede reimportar como traslado pareado" in dialog.message
        dialog.dismiss()
    page.once("dialog", reject_unpaired_csv)
    page.locator("#real-export-csv").click()
    assert not errors, errors
    browser.close()
    print("Cartera REAL: traslado pareado, coste e historial persistentes")
