"""Una copia local pública atrasada conserva el gráfico y avisa en ambas vistas."""

import os
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

from app.marketdata.archive import START, START_EUR
from app.marketdata.adapters.bitstamp import DAY


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000/ui/")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error) + "\n" + str(getattr(error, "stack", ""))))

    def archive(route):
        quote = parse_qs(urlparse(route.request.url).query).get("quote_currency", ["USD"])[0]
        first = START if quote == "USD" else START_EUR
        bars = [{"t": first + i * DAY, "o": "4", "h": "6", "l": "3",
                 "c": "5", "v": "2", "closed": True} for i in range(3)]
        route.fulfill(status=200, json={
            "available": True, "symbol": "BTC/" + quote,
            "resolved_provider": "bitstamp", "timeframe": "1d",
            "source": "Bitstamp OHLC público", "candles": bars,
            "count": len(bars), "first_candle_at": first,
            "last_candle_at": bars[-1]["t"], "missing_days": 0,
            "as_of_day": first + 8 * DAY, "complete_through": bars[-1]["t"],
            "cache_status": "local-stale", "stale": True,
            "note": "Archivo local sin actualización reciente; comprueba la fecha de la última vela.",
        })

    page.route("**/market/btc-daily-archive?*", archive)
    page.route("**/market/price?*", lambda route: route.fulfill(status=503, json={"detail": "offline"}))
    page.route("**/paper/market/preview?*", lambda route: route.fulfill(status=503, json={"detail": "offline"}))
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": False, "reason": "offline", "candles": [],
    }))
    page.goto(BASE + "#/estudio")
    page.locator("#st-pair").fill("BTC/USD")
    page.locator("#st-range").select_option("all")
    page.locator("#st-load").click()
    expect(page.locator("#st-archive-meta")).to_contain_text("Archivo 2012-01-01", timeout=20000)
    expect(page.locator("#st-archive-meta")).to_contain_text("Copia local sin actualizar")
    expect(page.locator("#st-chart canvas").first).to_be_visible()
    assert not errors, ("estudio", errors)

    page.goto(BASE + "#/inicio")
    page.locator("#home-range").select_option("all")
    expect(page.locator("#home-market .amber")).to_contain_text("Histórico desactualizado", timeout=20000)
    expect(page.locator("#home-market .amber")).to_contain_text("Archivo local sin actualización")
    expect(page.locator("#home-market canvas").first).to_be_visible()
    assert not errors, ("inicio", errors)

    page.goto(BASE + "#/operativa")
    page.locator("#t-range").select_option("all")
    expect(page.locator("#t-market")).to_contain_text("No se puede operar", timeout=20000)
    expect(page.locator("#t-chart .amber")).to_contain_text("Histórico desactualizado")
    expect(page.locator("#t-chart canvas").first).to_be_visible()
    assert not errors, ("operativa", errors)

    page.goto(BASE + "#/portfolio")
    page.locator("#p-tf").select_option("all")
    page.locator("#p-load").click()
    expect(page.locator("#p-market .amber")).to_contain_text("Histórico desactualizado", timeout=20000)
    expect(page.locator("#p-market canvas").first).to_be_visible()
    page.wait_for_timeout(750)
    assert not errors, errors
    browser.close()
