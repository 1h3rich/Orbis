"""Comprueba que desplazar la gráfica pide velas anteriores, no posteriores."""

import json
import os
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
HOUR = 3600
LAST = (int(time.time()) // HOUR - 2) * HOUR
FIRST = LAST - 499 * HOUR


def candles(start, count):
    return [
        {"t": start + n * HOUR, "o": "100", "h": "102", "l": "99",
         "c": "101", "v": "2", "closed": True}
        for n in range(count)
    ]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    boundaries = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_candles(route):
        params = parse_qs(urlparse(route.request.url).query)
        bound = params.get("before")
        if bound:
            boundaries.append(int(bound[0]))
            rows = candles(FIRST - 200 * HOUR, 200) if len(boundaries) == 1 else []
        else:
            rows = candles(FIRST, 500)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "available": True, "symbol": "BTC/EUR", "resolved_provider": "mexc",
            "timeframe": "1h", "candles": rows,
            "older_pagination_supported": True,
            "history_exhausted": not rows,
        }))

    page.route("**/market/candles?*", serve_candles)
    page.goto(BASE_URL + "/ui/#/estudio")
    page.wait_for_load_state("networkidle")
    page.locator("#st-chart").wait_for(state="visible")
    box = page.locator("#st-chart").bounding_box()
    y = box["y"] + box["height"] * 0.4
    for _ in range(10):
        page.mouse.move(box["x"] + 180, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] - 90, y, steps=12)
        page.mouse.up()
        if boundaries:
            break
    page.wait_for_timeout(300)
    assert boundaries and boundaries[0] == FIRST, boundaries
    assert page.locator("#st-chart").is_visible()
    assert "BTC/EUR" in page.locator("#st-meta").inner_text()
    assert not errors, errors
    browser.close()
    print("Estudio: paginación anterior exclusiva y gráfico conservado")
