"""Comprobar que PAPER no presenta un coste histórico como precio actual."""

import json
import os

from playwright.sync_api import expect, sync_playwright


BASE_URL = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000")


def portfolio(stale):
    return {
        "quote_currency": "EUR", "cash_balance": "700", "cash_weight": "70",
        "positions_value": "300", "equity": "1000", "total_pnl": "0",
        "return_percentage": "0", "valuation_status":
        "ESTIMATED" if stale else "CURRENT_REFERENCE",
        "positions": [{
            "asset": "BTC", "quote_currency": "EUR", "quantity": "0.003",
            "average_cost": "100000", "invested": "300",
            "current_price": "100000", "value": "300",
            "unrealized_pnl": "0", "realized_pnl": "0", "weight_percentage": "30",
            "price_source": "last_trade" if stale else "kraken",
            "price_as_of": "2026-10-20T12:00:00+00:00",
            "price_age_seconds": None if stale else 1,
            "price_stale": stale,
        }],
    }


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    valuation = {"stale": True}

    def serve_portfolio(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(portfolio(valuation["stale"])))

    page.route("**/paper/portfolio?*", serve_portfolio)
    page.route("**/paper/operations?*", lambda route: route.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/market/**", lambda route: route.fulfill(
        status=503, content_type="application/json", body="{}"))

    page.goto(BASE_URL + "/ui/#/portfolio")
    page.wait_for_load_state("networkidle")
    assert page.locator("#p-hero .pf-label").inner_text() == "VALOR PAPER ESTIMADO"
    expect(page.locator("#p-body")).to_contain_text("Resultado PAPER estimado")
    assert page.locator("#p-hero .pf-valuation-note").inner_text() == \
        "1 posición sin cotización reciente."
    price = page.locator('#p-body td[data-label="Precio ref."]').first
    assert "última operación PAPER" in price.get_attribute("data-help")
    assert "*" in price.inner_text()
    assert "Valoración usando Kraken" not in page.locator("#p-body").inner_text()
    page.screenshot(path="/tmp/orbis-paper-valoracion-estimada.png", full_page=True)

    page.goto(BASE_URL + "/ui/#/inicio")
    page.wait_for_load_state("networkidle")
    page.locator("#home-stats .stat-label").first.wait_for()
    assert page.get_by_role("heading", name="Simulación PAPER").is_visible()
    assert "VALOR PAPER ESTIMADO" in page.locator("#home-stats").inner_text(), page.locator("#home-stats").inner_text()
    assert "PATRIMONIO ESTIMADO" not in page.locator("#home-stats").inner_text()
    assert "TIPO DE CUENTA" not in page.locator("#home-stats").inner_text()

    valuation["stale"] = False
    page.goto(BASE_URL + "/ui/#/portfolio")
    page.wait_for_load_state("networkidle")
    assert page.locator("#p-hero .pf-label").inner_text() == "VALOR PAPER DE REFERENCIA"
    assert page.locator("#p-hero .pf-valuation-note").count() == 0
    assert "Kraken" in page.locator('#p-body td[data-label="Precio ref."]').first.get_attribute("data-help")
    assert not errors, errors
    browser.close()
    print("PAPER: valoración estimada/reciente distinguida en cartera e inicio")
