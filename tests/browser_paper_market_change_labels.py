"""La variación BTC de mercado no se presenta como P/L PAPER ni usa OHLC atrasado."""

from datetime import datetime, timezone
import os
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000/ui/")
NOW = int(datetime.now(timezone.utc).timestamp())
PORTFOLIO = {
    "quote_currency": "EUR", "cash_balance": "700", "cash_weight": "70",
    "positions_value": "300", "equity": "1000", "total_pnl": "0",
    "return_percentage": "0", "valuation_status": "ESTIMATED",
    "positions": [{
        "asset": "BTC", "quote_currency": "EUR", "quantity": "0.003",
        "average_cost": "100000", "invested": "300", "current_price": "100000",
        "value": "300", "unrealized_pnl": "0", "realized_pnl": "0",
        "weight_percentage": "30", "price_source": "manual",
        "price_as_of": None, "price_age_seconds": None, "price_stale": True,
    }],
}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1360, "height": 850})
    errors = []
    history_state = {"stale": True, "short": False}
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/paper/portfolio?*", lambda route: route.fulfill(status=200, json=PORTFOLIO))
    page.route("**/paper/operations?*", lambda route: route.fulfill(status=200, json=[]))
    page.route("**/market/price?*", lambda route: route.fulfill(status=503, json={"detail": "offline"}))

    def history(route):
        timeframe = parse_qs(urlparse(route.request.url).query).get("timeframe", ["24h"])[0]
        step = 86400 if timeframe == "7d" else 3600
        count = 8 if timeframe == "7d" else 25
        points = [{"t": NOW - (count - 1 - index) * step,
                   "close": str(100 + index)} for index in range(count)]
        if history_state["short"]:
            points = points[-2:]
        route.fulfill(status=200, json={
            "source": "kraken", "stale": history_state["stale"],
            "points": points,
        })

    page.route("**/market/history?*", history)
    page.goto(BASE + "#/portfolio")
    page.wait_for_load_state("networkidle")
    cell = page.locator('#p-body td[data-chg="BTC"]')
    expect(page.locator("#p-body th")).to_contain_text(["Activo", "Cantidad", "Precio medio",
                                                         "Precio ref.", "Valor", "Mercado 24h",
                                                         "P/L", "Peso"])
    expect(cell).to_have_text("—")
    expect(page.locator("#p-evo")).to_contain_text("histórico reciente no disponible")

    history_state["stale"] = False
    page.reload()
    page.wait_for_load_state("networkidle")
    cell = page.locator('#p-body td[data-chg="BTC"]')
    expect(cell).to_contain_text("+24,00%")
    assert "no es P/L" in cell.get_attribute("data-help")
    expect(page.locator("#p-evo")).to_contain_text("BTC mercado 7d")
    expect(page.locator("#p-evo")).to_contain_text("Datos de Kraken")
    assert "evolución ni P/L" in page.locator("#p-evo p").get_attribute("data-help")

    history_state["short"] = True
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator('#p-body td[data-chg="BTC"]')).to_have_text("—")
    expect(page.locator("#p-evo")).to_contain_text("cobertura histórica insuficiente")
    assert not errors, errors
    browser.close()
