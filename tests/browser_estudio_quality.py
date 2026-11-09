"""El gráfico avisa de huecos sin dibujar velas ni precios ficticios."""

import json
import os
import time

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000")
HOUR = 3600
START = (int(time.time()) // HOUR - 120) * HOUR
ROWS = [
    {"t": START + (i + (1 if i >= 30 else 0)) * HOUR,
     "o": "100", "h": "102", "l": "99", "c": "101", "v": "2", "closed": True}
    for i in range(80)
]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_candles(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "available": True, "symbol": "BTC/EUR", "resolved_provider": "mexc",
            "timeframe": "1h", "candles": ROWS,
            "older_pagination_supported": False,
            "gap_count": 1, "first_gap": {"from": START + 30 * HOUR,
                                         "to": START + 31 * HOUR},
        }))

    page.route("**/market/candles?*", serve_candles)
    page.goto(BASE_URL + "/ui/#/estudio")
    page.wait_for_load_state("networkidle")
    badge = page.locator("#st-quality")
    badge.wait_for(state="visible")
    assert badge.inner_text() == "⚠ 1 tramo sin velas"
    assert "No se inventan precios" in badge.get_attribute("title")
    assert page.locator("#st-chart").is_visible()
    assert not errors, errors
    browser.close()
    print("Estudio: hueco visible, gráfico conservado, sin errores JS")
