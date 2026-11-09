"""No se atribuye el cambio 24h de un gráfico Binance al precio Kraken.

Mercado y cotización PAPER interceptados; no hay consultas al exchange ni
escrituras en carteras locales.
"""

from datetime import datetime, timezone
import os
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

from browser_paper_preview_mock import mock_preview


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000/ui/")
HOUR = int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
ROWS = [
    {"t": HOUR - (24 - n) * 3600, "o": str(100 + n), "h": str(103 + n),
     "l": str(99 + n), "c": str(101 + n), "v": "2", "closed": n < 24}
    for n in range(25)
]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    price_source = {"value": "kraken"}
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("localStorage.setItem('orbis.provider.default','binance')")
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "123", "source": price_source["value"], "age_seconds": 1, "stale": False,
    }))
    page.route("**/paper/market/preview?*", mock_preview)

    def candles(route):
        query = parse_qs(urlparse(route.request.url).query)
        assert query.get("provider") == ["binance"], route.request.url
        limit = int(query.get("limit", [0])[0])
        assert limit in (25, 61, 169), route.request.url
        step = 60 if limit == 61 else 3600
        rows = ROWS if limit == 25 else [
            {"t": HOUR - (limit - 1 - n) * step, "o": str(100 + n),
             "h": str(103 + n), "l": str(99 + n), "c": str(101 + n),
             "v": "2", "closed": n < limit - 1}
            for n in range(limit)
        ]
        route.fulfill(status=200, json={
            "available": True, "symbol": "BTC/EUR", "resolved_provider": "binance",
            "timeframe": query.get("timeframe", ["1h"])[0], "candles": rows,
            "older_pagination_supported": False,
        })

    page.route("**/market/candles?*", candles)
    page.route("**/market/history?*", lambda route: route.fulfill(status=200, json={
        "points": [], "source": "kraken", "stale": False,
    }))
    page.route("**/paper/portfolio?*", lambda route: route.fulfill(status=404, json={"detail": "none"}))
    page.route("**/paper/operations?*", lambda route: route.fulfill(status=200, json=[]))

    for path, root, note_root in (("#/inicio", "#home-market", "#home-market"),
                                  ("#/operativa", "#t-market", "#t-chart"),
                                  ("#/portfolio", "#p-market", "#p-market")):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        expect(page.locator(root + " .ticker-sub")).to_contain_text("24h: —")
        expect(page.locator(note_root + " .amber")).to_contain_text("Fuentes distintas")
        expect(page.locator(note_root + " .amber")).to_contain_text("Binance")

    price_source["value"] = "binance"
    page.goto(BASE + "#/inicio")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#home-market .ticker-sub")).to_contain_text("24h: +")
    assert page.locator("#home-market .amber").count() == 0
    with page.expect_request(lambda request: "/market/candles?" in request.url and "limit=169" in request.url):
        page.locator("#home-range").select_option("7d")
    with page.expect_request(lambda request: "/market/candles?" in request.url and "limit=61" in request.url):
        page.locator("#home-range").select_option("1h")

    assert not errors, errors
    browser.close()
