"""Usuario registra USDT que ya llegó a MEXC y recupera una respuesta perdida."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.browser_network import route_once
from tests.test_mexc_execution_state import PASSWORD


base = os.environ["ORBIS_TEST_BASE_URL"]
output = Path(os.environ["ORBIS_TEST_ROUNDTRIP_DIRECTORY"])
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440, "height":1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + "/ui/#/real")

    def unlock():
        page.locator("#real-passphrase").fill(PASSWORD)
        page.get_by_role("button", name="Desbloquear", exact=True).click()
        expect(page.locator("#real-account")).to_be_visible()

    unlock()
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button", name="Consultar MEXC", exact=True).click()
    trading = page.locator("#real-mexc-trading")
    trading.locator(":scope > details > summary").click()
    trading.get_by_text("Configurar límites manuales", exact=True).click()
    limits = trading.locator("[data-trading-limits]")
    for name, value in {"base_asset":"BTC", "max_order_quote":"30", "max_daily_turnover_quote":"100",
                        "max_exposure_quote":"50", "max_daily_loss_quote":"5",
                        "max_fee_bps":"20", "max_slippage_bps":"30"}.items():
        limits.locator(f'[name="{name}"]').fill(value)
    limits.get_by_role("button", name="Guardar límites manuales", exact=True).click()
    expect(trading.locator("[data-trading-limits-review]")).to_contain_text("Máx. por orden 30 USDT")
    activation_requests = []
    page.on("request", lambda request: activation_requests.append(request.url)
            if request.url.endswith(("/activate", "/resume")) else None)
    opening = trading.locator("[data-trading-opening-report]")
    review_balance = trading.get_by_role("button", name="Revisar saldo", exact=True)
    approve = trading.get_by_role("button", name="Habilitar operativa", exact=True)
    with page.expect_response("**/mexc-spot-parity/observations") as unequal:
        review_balance.click()
    assert unequal.value.ok and unequal.value.json()["blockers"] == ["QUANTITY_DIFFERENCE"]
    expect(opening).to_contain_text("40.125")
    expect(approve).to_be_hidden()
    trading.get_by_role("button", name="Buscar ingreso MEXC", exact=True).click()
    expect(page.locator("#real-mexc-transfers")).to_be_focused()
    page.get_by_role("button", name="Consultar y guardar", exact=True).click()
    panel = page.locator("#real-mexc-transfer-result")
    panel.get_by_text("Registrar ingresos USDT", exact=True).click()
    panel.get_by_role("checkbox", name="Ingreso USDT 1").check()
    panel.get_by_role("button", name="Revisar ingresos", exact=True).click()
    expect(panel).to_contain_text("700 → 740.125")
    expect(panel).to_contain_text("Aportación · 40.125 USDT")
    confirm = panel.get_by_role("button", name="Registrar aportación", exact=True)
    expect(confirm).to_be_disabled()
    panel.get_by_role("checkbox", name="Fondos fuera de ORBIS").check()
    expect(confirm).to_be_enabled()

    def lose_reply(route):
        reply = route.fetch()
        assert reply.ok and reply.json()["status"] == "RECORDED", reply.text()
        route.abort()

    route_once(page, "**/mexc-external-deposits/commit", lose_reply)
    confirm.click()
    expect(panel).to_contain_text("Respuesta sin confirmar")
    with page.expect_response("**/mexc-external-deposits/commit") as repeated:
        panel.get_by_role("button", name="Reintentar registro", exact=True).click()
    assert repeated.value.ok and repeated.value.json()["status"] == "ALREADY_RECORDED"
    expect(panel).to_contain_text("Aportación ya registrada")
    expect(page.locator("#real-history").get_by_text("CONTRIBUTION", exact=True)).to_have_count(2)
    draft = trading.locator('[name="draft"]')
    draft.select_option(index=1)
    with page.expect_response("**/mexc-spot-parity/observations") as equal:
        review_balance.click()
    assert equal.value.ok and equal.value.json()["saved"] and not equal.value.json()["orders_enabled"]
    expect(opening).to_have_text("Saldo Spot coincide · BTCUSDT")
    expect(approve).to_be_hidden()
    expect(trading.get_by_role("button", name="Enviar orden real", exact=True)).to_be_disabled()
    sessions = trading.locator('[name="session"] option')
    seeded_sessions = sessions.evaluate_all("options => options.map(option => option.value)")
    page.locator("#real-mexc-trade-verify > summary").click()
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("fixture-trade")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button", name="Verificar Trade", exact=True).click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("autorización de operativa separada")
    expect(opening).to_be_hidden()
    review_balance.click()
    expect(approve).to_be_enabled()
    assert sessions.evaluate_all("options => options.map(option => option.value)") == seeded_sessions
    draft.select_option("")
    expect(approve).to_be_hidden()
    expect(opening).to_be_hidden()
    assert not activation_requests
    page.get_by_role("button", name="Bloquear", exact=True).click()
    unlock()
    expect(page.locator("#real-history").get_by_text("CONTRIBUTION", exact=True)).to_have_count(2)
    expect(page.locator("#real-history")).to_contain_text("40.125")
    expect(page.locator("#real-route-view-status")).to_have_text("Conecta View")
    page.get_by_role("button", name="Ingresos directos", exact=True).click()
    journey_panel = page.locator("#real-mexc-direct-journeys")
    selection = journey_panel.locator("[data-direct-journey]")
    expect(selection.locator("option")).to_have_count(2)
    with page.expect_response("**/mexc-external-deposits/journeys/*") as reviewed:
        selection.select_option(index=1)
    assert reviewed.value.ok and reviewed.value.request.method == "GET"
    assert reviewed.value.headers["cache-control"] == "no-store"
    report = journey_panel.locator("[data-direct-result]")
    expect(report).to_contain_text("Wallet de origen sin identificar")
    expect(report).to_contain_text("Ingreso · Contabilizado")
    expect(report).to_contain_text("40.125 USDT")
    expect(report).to_contain_text("Retirada BNB · Por solicitar")
    report.get_by_role("button", name="Abrir operativa Spot", exact=True).click()
    expect(page.locator("#real-mexc-trading > details > summary")).to_be_focused()
    expect(page.get_by_role("button", name="Enviar orden real", exact=True)).to_be_disabled()
    journey_panel.screenshot(path=str(output / "direct-journey.png"))
    page.locator("#real-hide").click()
    expect(page.locator("#real-hide")).to_have_text("Mostrar importes")
    page.get_by_role("button", name="Ingresos directos", exact=True).click()
    expect(selection.locator("option")).to_have_count(2)
    selection.select_option(index=1)
    expect(report).to_contain_text("Importes ocultos")
    expect(report).not_to_contain_text("40.125")
    page.set_viewport_size({"width":390, "height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors
    state = page.request.get(base + "/__test/state").json()
    assert state == {"orders":0, "wallet_sends":0, "withdrawals":0}
    (output / "external-deposit-result.json").write_text(json.dumps({
        "passed":True, "same_commit_replayed":True, "reopened":True,
        "provider_mutations":sum(state.values()), "direct_journey_offline":True, "direct_journey_masked":True,
        "direct_journey_opens_spot_without_view":True,
        "balance_review_separate":not activation_requests, "page_errors":errors}))
    browser.close()
