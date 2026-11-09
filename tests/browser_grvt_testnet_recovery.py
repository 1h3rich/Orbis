"""Panel GRVT testnet local con bóveda temporal y una respuesta de ejemplo."""

import json

from playwright.sync_api import expect, sync_playwright


URL = "http://127.0.0.1:8799"
SAMPLE = {
    "sub_account_id": "123456", "stopped": True, "can_submit": False,
    "items": [{
        "client_order_id": "9223372036854775809", "instrument": "BTC_USDT_Perp",
        "side": "BUY", "intended_size": "0.010000000000000001",
        "journal_status": "OBSERVED", "external_status": "CANCELLED",
        "external_traded_size": "0.004000000000000001",
        "cancel_status": "AUDITED", "cancel_external_status": "CANCELLED",
        "verified_audit_count": 1, "current_state_audited": True,
        "current_audit_findings": ["EXTERNAL_FILLS", "POSITION_DELTA_MISMATCH"],
        "next_check": "REVIEW_AUDIT_FINDINGS_NO_RELEASE",
        "signal_source": {"kind": "GRVT_AUTO_PAPER", "session_id": "session-123",
                          "action_request_id": "action-456", "action": "OPEN",
                          "direction": "LONG", "version_id": "12345678-1234-4000-8000-123456789abc",
                          "dataset_id": "dataset-123", "dataset_hash": "a" * 64,
                          "client_order_id": "9223372036854775809"},
        "price_guard": {"source": "GRVT_TESTNET_PUBLIC_TICKER", "bid": "49999",
                        "ask": "50000", "limit_price": "50000",
                        "event_time_ns": 1780000000000000000,
                        "max_spread_bps": 40, "max_slippage_bps": 25},
        "fill_summary": {"fill_count": 1,
                         "filled_size": "0.004000000000000001",
                         "sizes_match_order": True,
                         "weighted_average_price": "50000.000000000000000000",
                         "fees_by_currency": [["USDT", "0.10"]],
                         "causal_proof": False},
    }],
}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/real/exchanges/grvt/testnet/subaccounts", lambda route:
               route.fulfill(status=200, content_type="application/json", body=json.dumps({
                   "network": "TESTNET", "can_submit": False,
                   "sub_account_ids": ["123456"],
               })))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(URL + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("recovery browser secret 123")
    page.locator("#real-passphrase-confirm").fill("recovery browser secret 123")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator("#real-new-account").wait_for()
    page.wait_for_load_state("networkidle")
    no_account_form = page.locator("#real-grvt-testnet-recovery-form")
    expect(no_account_form.locator('[name="sub_account_id"]')).to_have_value("123456")
    assert page.locator("#real-grvt-testnet-subaccounts option").count() == 1
    no_account_form.get_by_role("button", name="Consultar registro").click()
    expect(page.locator("#real-grvt-testnet-recovery-result")).to_contain_text(
        "Sin intentos GRVT testnet guardados.")
    page.locator("#real-hide").click()
    page.get_by_role("button", name="Mostrar importes").wait_for()
    expect(page.locator("#real-grvt-testnet-recovery-result")).to_contain_text(
        "Sin intentos GRVT testnet guardados.")
    page.locator("#real-hide").click()
    page.get_by_role("button", name="Ocultar importes").wait_for()
    page.locator('#real-new-account [name="name"]').fill("Aislada")
    page.get_by_role("button", name="Crear cuenta").click()
    panel = page.locator(".real-grvt-testnet")
    panel.wait_for()
    assert "TESTNET · REVISIÓN" in panel.inner_text()
    form = page.locator("#real-grvt-testnet-recovery-form")
    form.locator('[name="sub_account_id"]').fill("123456")
    form.get_by_role("button", name="Consultar registro").click()
    expect(page.locator("#real-grvt-testnet-recovery-result")).to_contain_text(
        "Sin intentos GRVT testnet guardados.")

    current_report = {"value": SAMPLE}
    page.route("**/real/exchanges/grvt/testnet/recovery?*", lambda route:
               route.fulfill(status=200, content_type="application/json",
                             body=json.dumps(current_report["value"])))
    form.get_by_role("button", name="Consultar registro").click()
    result = page.locator("#real-grvt-testnet-recovery-result")
    result.locator("tbody tr").first.wait_for()
    assert "Revisar hallazgos" in result.inner_text()
    assert "Fills externos" in result.inner_text()
    assert "Posición distinta" in result.inner_text()
    assert "0.004000000000000001" in result.inner_text()
    assert "sin órdenes nuevas desde aquí" in result.inner_text()
    assert "PAPER · entrada · v12345678" in result.inner_text()
    quote = result.locator(".real-testnet-quote:not(.real-testnet-fills)")
    quote.locator("summary").click()
    expect(quote).to_contain_text("Bid 49999 · Ask 50000")
    expect(quote).to_contain_text("spread ≤40 bps · desliz. ≤25 bps")
    expect(quote).to_contain_text("no acredita fills")
    fills = result.locator(".real-testnet-fills")
    fills.locator("summary").click()
    expect(fills).to_contain_text("PM 50000.000000000000000000")
    expect(fills).to_contain_text("Comisión 0.10 USDT")
    expect(fills).to_contain_text("saldo no conciliado")
    assert "session-123" in result.locator("tbody tr td").first.locator("small.muted").get_attribute("title")
    actions = page.locator("#real-grvt-testnet-actions")
    result.get_by_role("button", name="Auditar").click()
    expect(actions).to_have_attribute("open", "")
    expect(page.locator("#real-grvt-testnet-action-result")).to_contain_text("Introduce una API key")
    audited = []

    def audit_route(route):
        audited.append(json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "network": "TESTNET", "sub_account_id": "123456",
            "client_order_id": "9223372036854775809", "journal_status": "OBSERVED",
            "external_status": "CANCELLED", "traded_size": "0.004000000000000001",
            "audit_id": "audit-id", "cancel_status": "AUDITED",
            "execution_unverified": True, "mainnet_order_sent": False,
        }))

    page.route("**/real/exchanges/grvt/testnet/orders/audit", audit_route)
    testnet_key = actions.locator("#real-grvt-testnet-key")
    testnet_key.fill("testnet-key-only-123")
    result.get_by_role("button", name="Auditar").click()
    expect(page.locator("#real-grvt-testnet-action-result")).to_contain_text("Auditoría guardada")
    expect(testnet_key).to_have_value("")
    assert audited[0]["api_key"] == "testnet-key-only-123"
    assert audited[0]["confirmation"] == "AUDITAR_GRVT_TESTNET"
    assert "testnet-key-only-123" not in page.locator(".real-grvt-testnet").inner_text()

    pending = json.loads(json.dumps(SAMPLE))
    pending.update(stopped=False)
    pending["items"][0].update(
        journal_status="POST_CLAIMED", external_status=None,
        external_traded_size=None, cancel_status=None, cancel_external_status=None,
        verified_audit_count=0, current_state_audited=False,
        current_audit_findings=[], next_check="LOOKUP_ORDER_BY_CLIENT_ID")
    current_report["value"] = pending
    cancelled = []

    def cancel_route(route):
        cancelled.append(json.loads(route.request.post_data))
        current_report["value"] = json.loads(json.dumps(pending))
        current_report["value"].update(stopped=True)
        current_report["value"]["items"][0].update(
            journal_status="OBSERVED", external_status="OPEN",
            external_traded_size="0", cancel_status="RESPONSE_OBSERVED",
            next_check="AUDIT_AFTER_CANCEL")
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "network":"TESTNET", "sub_account_id":"123456",
            "client_order_id":"9223372036854775809", "stopped":True,
            "cancel_status":"RESPONSE_OBSERVED", "external_status":"OPEN",
            "execution_unverified":True, "mainnet_order_sent":False,
        }))

    page.route("**/real/exchanges/grvt/testnet/orders/stop-cancel", cancel_route)
    form.get_by_role("button", name="Consultar registro").click()
    cancel_button = result.get_by_role("button", name="Parar + cancelar")
    cancel_button.wait_for()
    testnet_key.fill("testnet-key-only-456")
    page.once("dialog", lambda dialog: dialog.accept())
    cancel_button.click()
    expect(page.locator("#real-grvt-testnet-action-result")).to_contain_text("OPEN. Audita el ID")
    expect(testnet_key).to_have_value("")
    assert len(cancelled) == 1 and cancelled[0]["confirmation"] == "DETENER_Y_CANCELAR_GRVT_TESTNET"
    expect(result).to_contain_text("detenida")
    assert result.get_by_role("button", name="Parar + cancelar").count() == 0
    current_report["value"] = json.loads(json.dumps(pending))
    form.get_by_role("button", name="Consultar registro").click()
    expect(result).to_contain_text("sin parada registrada")
    stopped = []

    def stop_route(route):
        stopped.append(json.loads(route.request.post_data))
        current_report["value"].update(stopped=True)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "network":"TESTNET", "sub_account_id":"123456", "stopped":True,
            "external_orders_cancelled":False, "mainnet_order_sent":False,
        }))

    page.route("**/real/exchanges/grvt/testnet/subaccounts/123456/stop", stop_route)
    stop_button = actions.get_by_role("button", name="Parar subcuenta")
    expect(stop_button).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    stop_button.click()
    expect(page.locator("#real-grvt-testnet-action-result")).to_contain_text(
        "Las órdenes externas no se cancelaron")
    expect(stop_button).to_be_disabled()
    assert stopped == [{"confirmation":"DETENER_GRVT_TESTNET"}]
    page.screenshot(path="/tmp/orbis-grvt-testnet-recovery-ui.png", full_page=True)

    rejected = json.loads(json.dumps(SAMPLE))
    rejected["items"][0].update(
        journal_status="OBSERVED", external_status="REJECTED", external_traded_size="0",
        cancel_status=None, cancel_external_status=None, verified_audit_count=2,
        current_audit_findings=[], next_check="REVIEW_AUDIT_NO_RELEASE")
    current_report["value"] = rejected
    submitted = []

    def close_route(route):
        submitted.append(json.loads(route.request.post_data))
        current_report["value"] = json.loads(json.dumps(rejected))
        current_report["value"]["items"][0].update(
            journal_status="REJECTED_CLOSED", next_check="REJECTED_ZERO_FILL_ARCHIVED")
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "network": "TESTNET", "client_order_id": "9223372036854775809",
            "journal_status": "REJECTED_CLOSED", "can_submit": False,
            "new_order_sent": False,
        }))

    page.route("**/real/exchanges/grvt/testnet/rejected-zero-fill/close", close_route)
    form.get_by_role("button", name="Consultar registro").click()
    close_button = result.get_by_role("button", name="Cerrar rechazo")
    close_button.wait_for()
    page.once("dialog", lambda dialog: dialog.accept())
    close_button.click()
    expect(result).to_contain_text("Rechazo sin fills cerrado")
    assert result.get_by_role("button", name="Cerrar rechazo").count() == 0
    assert submitted == [{"sub_account_id": "123456",
                          "client_order_id": "9223372036854775809",
                          "confirmation": "CERRAR_RECHAZO_TESTNET"}]

    page.locator("#real-hide").click()
    page.get_by_role("button", name="Mostrar importes").wait_for()
    page.wait_for_load_state("networkidle")
    form = page.locator("#real-grvt-testnet-recovery-form")
    form.locator('[name="sub_account_id"]').fill("123456")
    form.get_by_role("button", name="Consultar registro").click()
    page.locator("#real-grvt-testnet-recovery-result tbody tr").first.wait_for()
    page.locator(".real-testnet-quote:not(.real-testnet-fills) summary").click()
    page.locator(".real-testnet-fills summary").click()
    assert "0.004000000000000001" not in page.locator("#real-grvt-testnet-recovery-result").inner_text()
    assert "50000.000000000000000000" not in page.locator("#real-grvt-testnet-recovery-result").inner_text()
    assert "49999" not in page.locator("#real-grvt-testnet-recovery-result").inner_text()
    assert "••••" in page.locator("#real-grvt-testnet-recovery-result").inner_text()
    page.locator("#real-lock").click()
    page.locator("#real-passphrase").wait_for()
    assert page.locator("#real-grvt-testnet-recovery-result").count() == 0
    assert not errors, errors
    print("Browser GRVT testnet recovery passed: locked, local, findings, hidden amounts")
    browser.close()
