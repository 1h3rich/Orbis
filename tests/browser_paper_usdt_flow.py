"""La UI PAPER separa USDT de EUR y nunca lo etiqueta con el símbolo euro."""

import json
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def portfolio(quote):
    usdt = quote == "USDT"
    return {
        "quote_currency": quote, "initial_balance": "1000",
        "cash_balance": "700" if usdt else "1000",
        "cash_weight": "67.96" if usdt else "100",
        "positions_value": "330" if usdt else "0",
        "equity": "1030" if usdt else "1000",
        "realized_pnl": "0", "unrealized_pnl": "30" if usdt else "0",
        "total_pnl": "30" if usdt else "0",
        "return_percentage": "3" if usdt else "0", "total_fees": "0",
        "valuation_status": "ESTIMATED", "positions": ([{
            "asset": "BTC", "quote_currency": "USDT", "quantity": "0.003",
            "average_cost": "100000", "invested": "300",
            "current_price": "110000", "price_source": "manual",
            "price_as_of": None, "price_age_seconds": None, "price_stale": True,
            "value": "330", "unrealized_pnl": "30", "realized_pnl": "0",
            "return_percentage": "10", "fees": "0", "weight_percentage": "32.04",
        }] if usdt else []),
    }


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_portfolio(route):
        quote = parse_qs(urlsplit(route.request.url).query).get("quote_currency", ["EUR"])[0]
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(portfolio(quote)))

    page.route("**/paper/portfolio?*", serve_portfolio)
    page.route("**/paper/operations?*", lambda route: route.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/market/**", lambda route: route.fulfill(
        status=503, content_type="application/json", body="{}"))
    page.goto("http://127.0.0.1:8000/ui/#/portfolio")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#p-quote option[value='USDT']")).to_have_count(1)
    page.locator("#p-quote").select_option("USDT")
    expect(page.locator("#p-hero .pf-value")).to_contain_text("1030,00 USDT")
    expect(page.locator("#p-body")).to_contain_text("110.000,00 USDT")
    assert "€" not in page.locator("#p-hero").inner_text()
    assert "€" not in page.locator("#p-body").inner_text()
    page.locator("#p-quote").select_option("EUR")
    expect(page.locator("#p-hero .pf-value")).to_contain_text("1000,00")
    assert "USDT" not in page.locator("#p-hero").inner_text()
    page.goto("http://127.0.0.1:8000/ui/#/operativa")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#t-quote option[value='USDT']")).to_have_count(1)
    page.goto("http://127.0.0.1:8000/ui/#/estrategias")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#f-strat select[name='quote'] option[value='USDT']")).to_have_count(1)
    page.goto("http://127.0.0.1:8000/ui/#/ledger")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#f-filter select[name='quote'] option[value='USDT']")).to_have_count(1)
    page.goto("http://127.0.0.1:8000/ui/#/inicio")
    page.wait_for_load_state("networkidle")
    page.locator("#home-quote").select_option("USDT")
    expect(page.locator("#home-market-heading")).to_have_text("BTC/USDT")
    expect(page.locator("#home-stats")).to_contain_text("1030,00 USDT")
    assert "€" not in page.locator("#home-stats").inner_text()
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#home-quote")).to_have_value("USDT")
    expect(page.locator("#home-market-heading")).to_have_text("BTC/USDT")
    expect(page.locator("#home-stats")).to_contain_text("1030,00 USDT")
    page.screenshot(path="/tmp/orbis-usdt-inicio.png", full_page=True)
    assert not errors, errors
    browser.close()
