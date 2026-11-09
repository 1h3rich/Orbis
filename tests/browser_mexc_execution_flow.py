"""Fallo antes de envío → liberar → comprar → STOP → revisar → vender por UI."""

import os

from playwright.sync_api import expect, sync_playwright

from tests.browser_network import route_once
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
EXISTING = os.environ.get("ORBIS_TEST_MEXC_EXISTING") == "1"


def open_inventory(page, panel, selected, other):
    shortcut = panel.get_by_role("button", name="Registrar saldo inicial", exact=True)
    panel.locator(":scope > details > summary").click()
    expect(shortcut).to_be_visible()

    def prepare_preview():
        shortcut.click()
        expect(page.locator("#real-mexc-opening > summary")).to_be_focused()
        page.get_by_role("button", name="Leer saldos", exact=True).click()
        form = page.locator("#real-mexc-opening-form")
        expect(form.locator('[data-opening-asset="USDT"]')).to_be_disabled()
        form.locator('[data-opening-asset="BTC"]').fill("60000.123456789")
        form.locator('[data-opening-asset="ETH"]').fill("2400")
        form.locator('[name="costs_ack"]').check()
        form.get_by_role("button", name="Vista previa", exact=True).click()
        expect(page.locator("#real-mexc-opening-preview")).to_contain_text("120.000246913578")
        commit = page.locator("#real-mexc-opening-commit")
        commit.locator('[name="confirmation"]').fill("IMPORTAR_SALDO_INICIAL")
        return commit

    prepare_preview()
    page.locator("#real-account").select_option(other)
    expect(page.locator("#real-mexc-opening-form")).to_have_count(0)
    expect(page.locator("#real-mexc-opening-commit")).to_have_count(0)
    expect(page.locator("#real-history")).to_contain_text("Todavía no hay movimientos.")
    page.locator("#real-account").select_option(selected)
    commit = prepare_preview()
    submissions = []
    page.on("request", lambda request: submissions.append(request.post_data_json)
            if request.method=="POST" and request.url.endswith("/mexc-opening/commit") else None)

    def lose_committed_reply(route):
        result = route.fetch()
        assert result.status==200 and result.json()["counts"]["new"]==3, result.text()
        route.abort()

    route_once(page,"**/mexc-opening/commit",lose_committed_reply)
    commit.get_by_role("button",name="Registrar inventario",exact=True).click()
    expect(commit.locator("[data-opening-commit-state]")).to_contain_text("Respuesta sin confirmar")
    expect(page.get_by_role("button",name="Leer saldos",exact=True)).to_be_disabled()
    assert page.request.post(BASE+"/__test/execution-fault",data={"move_quote":True}).ok
    with page.expect_response("**/mexc-opening/commit") as replay:
        commit.get_by_role("button",name="Reintentar registro",exact=True).click()
    assert replay.value.status == 200, replay.value.text()
    assert replay.value.json()["counts"] == {"total":3,"new":0,"already_present":3}
    assert len(submissions)==2 and submissions[0]==submissions[1]
    expect(page.locator("#real-notice")).to_contain_text("3 asiento(s) ya registrados")
    expect(page.locator("#real-history").get_by_text("CONTRIBUTION",exact=True)).to_have_count(1)
    expect(page.locator("#real-history").get_by_text("ADJUSTMENT_IN",exact=True)).to_have_count(2)
    expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(0)
    expect(shortcut).to_be_hidden()
    assert page.request.get(BASE+"/__test/posts").json()["count"]==0
    assert page.request.post(BASE+"/__test/execution-fault",data={"restore_quote":True}).ok


def save_manual_limits(panel):
    panel.get_by_text("Configurar límites manuales",exact=True).click()
    form = panel.locator("[data-trading-limits]")
    values = {"base_asset":"BTC", "max_order_quote":"150", "max_daily_turnover_quote":"500",
              "max_exposure_quote":"1000", "max_daily_loss_quote":"100",
              "max_fee_bps":"20", "max_slippage_bps":"50"}
    for name,value in values.items():
        form.locator(f'[name="{name}"]').fill(value)
    form.get_by_role("button",name="Guardar límites manuales",exact=True).click()
    expect(panel.locator('[name="draft"] option')).to_have_count(2)
    expect(panel.locator("[data-trading-limits-review]")).to_contain_text("150 USDT")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440, "height":1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear", exact=True).click()
    panel = page.locator("#real-mexc-trading")
    if EXISTING:
        selected = page.locator("#real-account").input_value()
        page.locator("#real-add-account > summary").click()
        page.locator('#real-add-account-form [name="name"]').fill("Otra cuenta")
        with page.expect_response(lambda response: response.url.endswith("/real/accounts") and response.request.method=="POST") as created:
            page.get_by_role("button",name="Crear cuenta",exact=True).click()
        other = created.value.json()["id"]
        expect(page.locator("#real-account")).to_have_value(other)
        page.locator("#real-account").select_option(selected)
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button", name="Consultar MEXC", exact=True).click()
    if EXISTING:
        open_inventory(page,panel,selected,other)
    page.locator("#real-mexc-trade-verify > summary").click()
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("fixture-trade")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button", name="Verificar Trade", exact=True).click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("autorización de operativa separada")
    if EXISTING:
        save_manual_limits(panel)
    else:
        panel.locator(":scope > details > summary").click()
    panel.locator('[name="draft"]').select_option(index=1)
    panel.get_by_role("button", name="Revisar saldo", exact=True).click()
    with page.expect_response("**/real/accounts/*/mexc-policy-drafts/*/activate") as activation:
        panel.get_by_role("button", name="Habilitar operativa", exact=True).click()
    assert activation.value.status == 200, activation.value.text()
    expect(panel.locator("[data-trading-state]")).to_contain_text("activa")
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 0
    if EXISTING:
        panel.locator('[name="budget"]').fill("65,09")
        with page.expect_response("**/mexc-trading-sessions/*/orders/review") as drafted:
            panel.get_by_role("button", name="Calcular orden", exact=True).click()
        assert drafted.value.request.post_data_json == {"side":"BUY", "budget_quote":"65.09"}
        assert drafted.value.ok and drafted.value.json()["compatible"], drafted.value.text()
        expect(panel.locator('[name="quantity"]')).to_have_value("0.001")
        expect(panel.locator('[name="price"]')).to_have_value("65001")
        expect(panel.locator('[data-trading-review-status]')).to_contain_text("Coste máximo est.")
        assert page.request.get(BASE + "/__test/posts").json()["count"] == 0
        panel.locator('[name="budget"]').fill("65.11")
        expect(panel.locator('[name="quantity"]')).to_have_value("")
        expect(panel.locator('[name="price"]')).to_have_value("")
        panel.locator('[name="budget"]').fill("65.09")
        panel.get_by_role("button", name="Calcular orden", exact=True).click()
        expect(panel.locator('[name="quantity"]')).to_have_value("0.001")
    else:
        panel.locator('[name="quantity"]').fill("0.001")
        panel.locator('[name="price"]').fill("65000")
    page.request.post(BASE + "/__test/execution-fault", data={"next_test":True})
    with page.expect_response("**/real/accounts/*/mexc-trading-sessions/*/orders") as failed:
        panel.get_by_role("button", name="Enviar orden real", exact=True).click()
    assert failed.value.status == 200, failed.value.text()
    assert failed.value.json()["stage"] == "REVIEW_REQUIRED", failed.value.text()
    assert failed.value.json()["journal_status"] == "RESERVED", failed.value.text()
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 0
    page.request.post(BASE + "/__test/execution-fault", data={"restore_quote":True})
    route = page.get_by_role("navigation",name="Wallet ↔ MEXC")
    expect(route.locator("#real-route-status-3")).to_have_text("Revisa la reserva")
    expect(panel.get_by_role("button",name="Enviar orden real",exact=True)).to_be_disabled()
    route.get_by_role("button",name="Orden Spot Compra / venta",exact=True).click()
    journal = page.locator("#real-mexc-execution")
    expect(journal.locator(":scope > summary")).to_be_focused()
    expect(journal.locator(".real-exec-card")).to_have_count(1)
    expect(journal.get_by_role("button",name="Sincronizar orden",exact=True)).to_have_count(0)
    with page.expect_response("**/real/execution/mexc/intents/*/release-unsent") as released:
        page.get_by_role("button", name="Liberar reserva", exact=True).click()
    assert released.value.status == 200, released.value.text()
    expect(journal).to_contain_text("Sin órdenes pendientes en esta cuenta.")
    expect(route.locator("#real-route-status-3")).to_have_text("Revisa saldo y límites")
    expect(panel.locator("[data-trading-state]")).to_contain_text("activa")
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 0
    with page.expect_response("**/real/accounts/*/mexc-trading-sessions/*/orders") as purchase:
        panel.get_by_role("button", name="Enviar orden real", exact=True).click()
    assert purchase.value.status == 200, purchase.value.text()
    assert purchase.value.json()["journal_status"] == "RESERVATION_CLOSED", purchase.value.text()
    if EXISTING:
        sent = purchase.value.request.post_data_json
        assert sent["quantity_base"] == "0.001" and sent["limit_price_quote"] == "65001"
        assert sent["max_spend_quote"] == "65.09" and "budget_quote" not in sent
    expect(panel.locator("[data-trading-state]")).to_contain_text("Orden ejecutada y registrada")
    expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(1)
    expect(panel.locator('[name="quantity"]')).to_have_value("")
    old_policy = panel.locator('[name="session"]').input_value()
    with page.expect_response("**/real/accounts/*/mexc-stop") as stopped:
        panel.get_by_role("button", name="Detener cuenta", exact=True).click()
    assert stopped.value.status == 200, stopped.value.text()
    expect(panel.locator("[data-trading-state]")).to_contain_text("Cuenta detenida")
    expect(panel.get_by_role("button", name="Enviar orden real", exact=True)).to_be_disabled()
    panel.locator('[name="draft"]').select_option(index=1)
    panel.get_by_role("button", name="Revisar saldo", exact=True).click()
    with page.expect_response("**/real/accounts/*/mexc-policy-drafts/*/resume") as reopened:
        panel.get_by_role("button", name="Reabrir operativa", exact=True).click()
    assert reopened.value.status == 200, reopened.value.text()
    expect(panel.locator("[data-trading-state]")).to_contain_text("activa")
    assert old_policy not in panel.locator('[name="session"] option').evaluate_all("items => items.map(item => item.value)")
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 1
    panel.locator('[name="side"]').select_option("SELL")
    if EXISTING:
        panel.locator('[name="budget"]').fill("58.5")
        with page.expect_response("**/mexc-trading-sessions/*/orders/review") as drafted:
            panel.get_by_role("button", name="Calcular orden", exact=True).click()
        assert drafted.value.ok and drafted.value.json()["compatible"], drafted.value.text()
        expect(panel.locator('[name="quantity"]')).to_have_value("0.0009")
        expect(panel.locator('[name="price"]')).to_have_value("65000")
        expect(panel.locator('[data-trading-review-status]')).to_contain_text("Neto est.")
        assert page.request.get(BASE + "/__test/posts").json()["count"] == 1
    else:
        panel.locator('[name="quantity"]').fill("0.0009")
    with page.expect_response("**/real/accounts/*/mexc-trading-sessions/*/orders") as sale:
        panel.get_by_role("button", name="Enviar orden real", exact=True).click()
    assert sale.value.status == 200, sale.value.text()
    assert sale.value.json()["journal_status"] == "RESERVATION_CLOSED", sale.value.text()
    assert "max_spend_quote" not in sale.value.request.post_data_json
    expect(page.locator("#real-history").get_by_text("SELL", exact=True)).to_have_count(1)
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 2
    panel.screenshot(path=os.path.join(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"],"mexc-recovery.png"))
    page.get_by_role("button", name="Bloquear", exact=True).click()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear", exact=True).click()
    expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(1)
    expect(page.locator("#real-history").get_by_text("SELL", exact=True)).to_have_count(1)
    if EXISTING:
        eth = page.locator("#real-positions tr").filter(has=page.get_by_text("ETH", exact=True))
        expect(eth).to_have_count(1)
        expect(eth.locator("td").nth(1)).to_have_text("1.25")
        expect(eth.locator("td").nth(2)).to_contain_text("2400")
    panel.locator(":scope > details > summary").click()
    expect(panel.get_by_role("button", name="Enviar orden real", exact=True)).to_be_disabled()
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 2
    page.set_viewport_size({"width":390, "height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    browser.close()
    if EXISTING:
        print("MEXC UI: cálculo BUY/SELL por importe sin envío, compra y venta BTC, ETH conservado al reabrir · transporte controlado")
    else:
        print("MEXC UI: reserva fallida liberada, compra, STOP, revisión, venta y persistencia OK · transporte ficticio")
