"""Una página corta de velas no equivale a fin del histórico paginado."""

import json
import os
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000")
HOUR = 3600
LAST = (int(time.time()) // HOUR - 2) * HOUR
FIRST = LAST - 99 * HOUR


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
    before_values = []
    requested_limits = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_candles(route):
        params = parse_qs(urlparse(route.request.url).query)
        requested_limits.append(int(params["limit"][0]))
        bound = params.get("before")
        if bound:
            before_values.append(int(bound[0]))
            rows = candles(FIRST - 50 * HOUR, 50) if len(before_values) == 1 else []
        else:
            rows = candles(FIRST, 100)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "available": True, "symbol": "BTC/EUR", "resolved_provider": "mexc",
            "timeframe": "1h", "candles": rows,
            "older_pagination_supported": True, "history_exhausted": not rows,
        }))

    page.route("**/market/candles?*", serve_candles)
    page.goto(BASE_URL + "/ui/#/estudio")
    page.wait_for_load_state("networkidle")
    assert requested_limits and 100 < requested_limits[0], requested_limits
    box = page.locator("#st-chart").bounding_box()
    y = box["y"] + box["height"] * 0.4
    for _ in range(25):
        page.mouse.move(box["x"] + 180, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] - 90, y, steps=12)
        page.mouse.up()
        if len(before_values) >= 2:
            break
        page.wait_for_timeout(100)
    assert before_values[:2] == [FIRST, FIRST - 50 * HOUR], before_values
    assert page.locator("#st-chart").is_visible()
    assert not errors, errors
    browser.close()
    print("Estudio: página corta → historia anterior → agotamiento explícito")
