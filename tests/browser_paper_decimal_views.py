"""La cartera y el historial PAPER muestran agregados grandes sin float."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"


def position(asset, value):
    return {
        "asset": asset, "quote_currency": "EUR", "quantity": "1",
        "average_cost": "100", "invested": "100", "current_price": value,
        "value": value, "unrealized_pnl": "0", "realized_pnl": "0",
        "weight_percentage": "50", "price_source": "manual",
        "price_as_of": None, "price_age_seconds": None, "price_stale": True,
    }


portfolio = {
    "quote_currency": "EUR", "cash_balance": "0", "cash_weight": "0",
    "positions_value": "246913578024691356.23", "equity": "246913578024691356.23",
    "total_pnl": "-0.000000000001", "return_percentage": "0",
    "valuation_status": "ESTIMATED",
    "positions": [position("BTC", "123456789012345678.11"),
                  position("ETH", "123456789012345678.12")],
}
operation = {
    "timestamp": "2025-01-01T00:00:00+00:00", "operation_type": "BUY",
    "asset": "BTC", "quote_currency": "EUR", "amount_spent": "100",
    "asset_received": "1", "price": "100", "exchange": "PAPER",
    "source": "MANUAL", "status": "EXECUTED", "realized_pnl": None,
    "pnl_pct": None, "trading_fee": "123456789012345678.01",
    "withdrawal_fee": "0.02", "network_fee": "0.03",
}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/paper/portfolio?*", lambda route: route.fulfill(status=200, json=portfolio))
    page.route("**/paper/operations/summary*", lambda route: route.fulfill(
        status=200, json={"operations_count": 1, "buys_count": 1, "sells_count": 0,
                          "volume": "100", "total_fees": "123456789012345678.06"}))
    page.route("**/paper/operations?*", lambda route: route.fulfill(
        status=200, json=([{"amount_spent": "123456789012345678.01"},
                           {"amount_spent": "0.02"}]
                          if "strategy_id=" in route.request.url else
                          [] if "asset=" in route.request.url else [operation])))
    page.route("**/strategies", lambda route: route.fulfill(status=200, json=[{
        "id": 1, "name": "Plan ficticio", "enabled": True,
        "config": {"asset": "BTC", "quote_currency": "EUR", "amount": "100",
                   "frequency": "mensual"},
    }]))
    page.route("**/market/**", lambda route: route.fulfill(status=503, json={}))
    page.goto(BASE + "/ui/#/portfolio")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#p-body tbody tr").first).to_contain_text("ETH/EUR")
    expect(page.locator("#p-hero .pf-pnl")).to_have_class("pf-pnl down")
    page.goto(BASE + "/ui/#/ledger")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#l-list [data-label='Comisión']")).to_contain_text(
        "123.456.789.012.345.678,06")
    assert page.evaluate("OrbisDecimal.sumStrings(['123456789012345678.01', '0.02', '0.03'])") == \
        "123456789012345678.06"
    page.goto(BASE + "/ui/#/estrategias")
    page.wait_for_load_state("networkidle")
    expect(page.locator('[data-exec="1"]')).to_contain_text(
        "2 ejecuciones · 123.456.789.012.345.678,03")
    assert not errors, errors
    browser.close()

print("PAPER: orden de cartera y comisiones exactas en navegador")
