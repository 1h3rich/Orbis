"""Interfaz de borradores MEXC con API controlada; nunca envía órdenes."""

import json
from datetime import datetime, timezone

from playwright.sync_api import expect, sync_playwright


drafts = {}
posts = []
order_tests = []
risk_reads = []
exposure_reads = []
versions = [
    {"id": "lab-long-v1", "name": "RSI recuperación", "version": 1,
     "definition": {"direction": "LONG"}},
    {"id": "lab-short-v1", "name": "Corto", "version": 1,
     "definition": {"direction": "SHORT"}},
]


def mock_lab(route):
    route.fulfill(status=200, content_type="application/json", body=json.dumps(versions))


def mock_drafts(route):
    account = route.request.url.split("/accounts/")[1].split("/")[0]
    if route.request.method == "POST":
        body = route.request.post_data_json
        assert body["strategy_version_id"] == "lab-long-v1"
        assert body["symbol"] == body["base_asset"] + body["quote_asset"]
        assert "enabled" not in body and "api_key" not in body
        assert datetime.fromisoformat(body["expires_at"]).astimezone(timezone.utc) > datetime.now(timezone.utc)
        posts.append(body)
        if len(posts) == 1:
            route.fulfill(status=503, content_type="application/json",
                          body=json.dumps({"detail": "Respuesta incierta de prueba"}))
            return
        drafts.setdefault(account, []).append({
            "id": body["request_id"], **body, "enabled": False,
            "execution_enabled": False, "status": "DRAFT_NOT_ACTIVATED"})
        route.fulfill(status=200, content_type="application/json", body=json.dumps(drafts[account][-1]))
    else:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(drafts.get(account, [])))


def mock_order_test(route):
    assert route.request.method == "POST"
    body = route.request.post_data_json
    assert body["quantity_base"] == "0.001"
    assert body["limit_price_quote"] == "100000"
    assert body["confirmation"] == "PROBAR_SIN_EJECUTAR"
    assert isinstance(body["request_id"], str) and len(body["request_id"]) == 36
    order_tests.append(body)
    route.fulfill(status=200, content_type="application/json", body=json.dumps({
        "id": body["request_id"], "status": "ACCEPTED", "persisted": True,
        "accepted_by_test_endpoint": True, "executed": False,
        "execution_enabled": False, "scope": "MEXC_ORDER_TEST_ONLY"}))


def mock_order_tests(route):
    route.fulfill(status=200, content_type="application/json", body=json.dumps([
        {"id": row["request_id"], "status": "ACCEPTED", "quantity_base": row["quantity_base"],
         "limit_price_quote": row["limit_price_quote"], "created_at": datetime.now(timezone.utc).isoformat()}
        for row in order_tests]))


def mock_daily_risk(route):
    assert route.request.method == "GET"
    risk_reads.append(route.request.url)
    reached = len(risk_reads) > 1
    route.fulfill(status=200, content_type="application/json", body=json.dumps({
        "turnover_quote": "270" if reached else "45",
        "max_daily_turnover_quote": "200",
        "realized_pnl_quote": "-32" if reached else "-6",
        "max_daily_loss_quote": "25",
        "quote_asset": "USDT",
        "base_asset": "BTC",
        "position_quantity_base": "0.5" if reached else "0",
        "position_cost_basis_quote": "50" if reached else "0",
        "average_cost_quote_per_base": "100" if reached else None,
        "cost_provenance": "UNLINKED_OR_CHANGED_TRADE",
        "book_turnover_limit_reached": reached,
        "book_realized_loss_limit_reached": reached,
        "execution_ready": False,
    }))


def mock_spot_exposure(route):
    assert route.request.method == "GET"
    exposure_reads.append(route.request.url)
    route.fulfill(status=200, content_type="application/json", body=json.dumps({
        "base_asset": "BTC", "quote_asset": "USDT",
        "base_exposure_quote": "150", "max_base_exposure_quote": "300",
        "other_assets_exposure_quote": "40", "base_limit_reached": False,
        "reconciled_with_book": False, "execution_ready": False,
    }))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/lab/strategy-versions", mock_lab)
    page.route("**/real/accounts/*/mexc-policy-drafts", mock_drafts)
    page.route("**/real/accounts/*/mexc-policy-drafts/*/order-test", mock_order_test)
    page.route("**/real/accounts/*/mexc-policy-drafts/*/order-tests", mock_order_tests)
    page.route("**/real/accounts/*/mexc-policy-drafts/*/daily-book-risk", mock_daily_risk)
    page.route("**/real/accounts/*/mexc-policy-drafts/*/spot-exposure", mock_spot_exposure)
    page.goto("http://127.0.0.1:8799/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.locator("#real-passphrase-confirm").fill("browser smoke secret 123")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator('#real-new-account [name="name"]').fill("Principal")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-mexc-policy-drafts > summary").click()
    expect(page.locator("#real-mexc-policy-form")).to_be_hidden()
    expect(page.locator("#real-mexc-policy-state")).to_contain_text("Conecta MEXC View")
    page.locator("#real-mexc-connect summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("browser-test-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("browser-test-secret")
    page.get_by_role("button", name="Consultar MEXC").click()
    expect(page.locator("#real-mexc-policy-state")).to_contain_text("Confirma primero")
    expect(page.locator("#real-mexc-policy-form")).to_be_hidden()
    page.locator("#real-mexc-link > summary").click()
    expect(page.locator("#real-mexc-link-form")).to_be_visible()
    page.locator('#real-mexc-link-form [name="confirmation"]').fill("VINCULAR")
    with page.expect_response(lambda response: response.request.method == "POST" and
                              response.url.endswith("/mexc-link")) as linked:
        page.get_by_role("button", name="Vincular libro").click()
    assert linked.value.status == 200
    expect(page.locator("#real-mexc-link-state")).to_contain_text("Vínculo local guardado")
    page.get_by_role("button", name="Revisar y guardar órdenes").click()
    expect(page.locator("#real-mexc-open-orders-result")).to_contain_text("ÓRDENES OBSERVADAS")
    expect(page.locator("#real-mexc-open-orders-result")).to_contain_text("ETHUSDT · 1")
    expect(page.locator("#real-mexc-open-orders-result")).to_contain_text("COBERTURA NO PROBADA")
    assert "browser-external-order" not in page.locator("#real-mexc-open-orders-result").inner_text()
    page.locator("#real-mexc-open-orders-history > summary").click()
    expect(page.locator("#real-mexc-open-orders-history-list button")).to_have_count(1)
    expect(page.locator("#real-mexc-open-orders-history-list")).to_contain_text("COBERTURA NO PROBADA")
    page.get_by_role("button", name="Revisar y guardar órdenes").click()
    expect(page.locator("#real-mexc-open-orders-history-list button")).to_have_count(2)
    page.get_by_role("button", name="Comparar últimas 2").click()
    expect(page.locator("#real-mexc-open-orders-compare-result")).to_contain_text("SIN CAMBIOS OBSERVADOS")
    expect(page.locator("#real-mexc-open-orders-compare-result")).to_contain_text("SIN CONCILIACIÓN")
    page.get_by_role("button", name="Comprobar y guardar").click()
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("COMPROBACIÓN BLOQUEADA")
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("NO CONCILIADO · SIN ÓRDENES")
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("Unidades distintas")

    def unstable_parity(route):
        response = route.fetch()
        body = response.json()
        body["book_stable"] = False
        body["blockers"] = ["BOOK_CHANGED_DURING_CHECK"]
        route.fulfill(response=response, body=json.dumps(body))

    page.route("**/mexc-spot-parity/observations", unstable_parity)
    page.get_by_role("button", name="Comprobar y guardar").click()
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("Repite la lectura")
    assert page.locator("#real-mexc-parity-result table").count() == 0
    page.unroute("**/mexc-spot-parity/observations", unstable_parity)
    archived_report = []

    def archived_parity(route):
        if route.request.method == "GET":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps([{**archived_report[0], "book_current": False,
                                            "recent": False}] if archived_report else []))
            return
        response = route.fetch()
        body = response.json()
        body.update(status="PARITY_OBSERVED_NOT_RECONCILED", blockers=[], saved=True,
                    locked_assets=[], book_stable=True, mexc_balances_stable=True)
        for asset in body["assets"]:
            asset["quantity_equal"] = True
        archived_report[:] = [body]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/mexc-spot-parity/observations", archived_parity)
    page.get_by_role("button", name="Comprobar y guardar").click()
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("EVIDENCIA GUARDADA")
    page.locator("#real-mexc-parity-history > summary").click()
    expect(page.locator("#real-mexc-parity-history-list button")).to_have_count(1)
    expect(page.locator("#real-mexc-parity-history-list")).to_contain_text("LIBRO CAMBIADO")
    page.locator("#real-mexc-parity-history-list button").click()
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("OBSERVACIÓN GUARDADA")
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("El libro cambió")
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("NO CONCILIADO · SIN ÓRDENES")
    page.unroute("**/mexc-spot-parity/observations", archived_parity)
    expect(page.locator("#real-mexc-policy-form")).to_be_visible()
    choices = page.locator('#real-mexc-policy-form [name="strategy_version_id"] option').all_inner_texts()
    assert any("RSI recuperación" in choice for choice in choices)
    assert all("Corto" not in choice for choice in choices)
    page.locator('#real-mexc-policy-form [name="strategy_version_id"]').select_option("lab-long-v1")
    page.locator('#real-mexc-policy-form [name="base_asset"]').fill("ETH")
    page.get_by_role("button", name="Guardar borrador").click()
    expect(page.locator("#real-mexc-policy-state")).to_contain_text("par debe coincidir")
    assert not posts
    page.locator('#real-mexc-policy-form [name="base_asset"]').fill("BTC")
    page.get_by_role("button", name="Guardar borrador").click()
    expect(page.locator("#real-mexc-policy-state")).to_contain_text("Respuesta incierta")
    page.get_by_role("button", name="Guardar borrador").click()
    expect(page.locator("#real-mexc-policy-list .real-policy-item")).to_have_count(1)
    expect(page.locator("#real-mexc-policy-list")).to_contain_text("DESACTIVADO")
    policy_risk = page.locator("#real-mexc-policy-list .real-policy-risk")
    policy_risk.get_by_role("button", name="Riesgo local").click()
    expect(policy_risk).to_contain_text("45/200 USDT")
    expect(policy_risk).to_contain_text("BTC 0 · PM — (sin posición)")
    expect(policy_risk).to_contain_text("coste manual · sin conciliar")
    expect(policy_risk).to_contain_text("sin conciliar")
    policy_risk.get_by_role("button", name="Riesgo local").click()
    expect(policy_risk).to_contain_text("LÍMITE LOCAL ALCANZADO")
    policy_risk.get_by_role("button", name="Exposición MEXC").click()
    expect(policy_risk).to_contain_text("BTC 150/300 USDT · otros activos 40 USDT · sin conciliar")
    test_form = page.locator("#real-mexc-policy-list .real-policy-test")
    test_form.locator('[name="quantity_base"]').fill("0.001")
    test_form.locator('[name="limit_price_quote"]').fill("100000")
    test_form.get_by_role("button", name="Probar BUY").click()
    expect(test_form.locator('[role="status"]')).to_contain_text("ninguna orden ejecutada")
    assert len(order_tests) == 1
    page.locator("#real-mexc-policy-list .real-policy-tests summary").click()
    expect(page.locator("#real-mexc-policy-list .real-policy-test-record")).to_have_count(1)
    expect(page.locator("#real-mexc-policy-list .real-policy-test-record")).to_contain_text("ACEPTADA · SIN EJECUCIÓN")
    assert len(posts) == 2 and posts[0] == posts[1]
    page.get_by_role("button", name="Ocultar importes").click()
    page.locator("#real-mexc-policy-drafts > summary").click()
    expect(page.locator("#real-mexc-policy-list")).to_contain_text("••••")
    page.locator("#real-mexc-policy-list .real-policy-risk").get_by_role("button", name="Riesgo local").click()
    expect(page.locator("#real-mexc-policy-list .real-policy-risk")).to_contain_text("••••")
    page.locator("#real-mexc-policy-list .real-policy-risk").get_by_role("button", name="Exposición MEXC").click()
    expect(page.locator("#real-mexc-policy-list .real-policy-risk [data-policy-exposure-state]")).to_contain_text("••••")
    assert "PM 100" not in page.locator("#real-mexc-policy-list .real-policy-risk").inner_text()
    assert "270" not in page.locator("#real-mexc-policy-list .real-policy-risk").inner_text()
    assert "150" not in page.locator("#real-mexc-policy-list .real-policy-risk").inner_text()
    assert len(exposure_reads) == 2
    assert "300" not in page.locator("#real-mexc-policy-list").inner_text()
    page.locator("#real-mexc-link > summary").click()
    page.get_by_role("button", name="Comprobar y guardar").click()
    expect(page.locator("#real-mexc-parity-result")).to_contain_text("••••")
    assert "0.26" not in page.locator("#real-mexc-parity-result").inner_text()
    page.get_by_role("button", name="Bloquear").click()
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-mexc-policy-drafts > summary").click()
    expect(page.locator("#real-mexc-policy-list .real-policy-item")).to_have_count(1)
    page.locator("#real-mexc-policy-list .real-policy-tests summary").click()
    expect(page.locator("#real-mexc-policy-list .real-policy-test-record")).to_have_count(1)
    expect(page.locator("#real-mexc-policy-form")).to_be_hidden()
    page.locator("#real-mexc-link > summary").click()
    expect(page.locator("#real-mexc-link-state")).to_contain_text("Conecta MEXC View")
    page.locator("#real-mexc-open-orders-history > summary").click()
    expect(page.locator("#real-mexc-open-orders-history-list button")).to_have_count(2)
    assert not errors, errors
    browser.close()
