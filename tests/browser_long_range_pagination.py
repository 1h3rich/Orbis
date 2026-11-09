"""Inicio/Operativa/PAPER comparten histórico largo paginado sin mezclar fuentes."""

import re
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright
from browser_paper_preview_mock import mock_preview


BASE = "http://127.0.0.1:8799/ui/"
DAY = 86400
TODAY = int(time.time()) // DAY * DAY
calls = []


def bars(last, count, step):
    return [{"t": last - (count - 1 - index) * step, "o": "100", "h": "102",
             "l": "99", "c": "101", "v": "10", "closed": True}
            for index in range(count)]


def mock_candles(route):
    query = parse_qs(urlparse(route.request.url).query)
    calls.append(query)
    daily = query["timeframe"] == ["1d"]
    before = int(query["before"][0]) if "before" in query else None
    if daily and before is not None:
        payload = bars(before - DAY, 1000, DAY)
    elif daily:
        payload = bars(TODAY - DAY, 1000, DAY)
    else:
        payload = bars(TODAY - 3600, 24, 3600)
    route.fulfill(status=200, json={"available": True, "resolved_provider": "mexc",
                                    "older_pagination_supported": True, "gap_count": 0,
                                    "candles": payload})


def point_count(locator):
    match = re.search(r"([\d.]+) puntos", locator.text_content())
    assert match, locator.text_content()
    return int(match.group(1).replace(".", ""))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("localStorage.setItem('orbis.provider.default', 'mexc')")
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "101", "source": "mexc", "age_seconds": 1, "stale": False}))
    page.route("**/paper/market/preview?*", mock_preview)
    page.route("**/market/candles?*", mock_candles)
    page.goto(BASE + "#/operativa")
    page.wait_for_load_state("networkidle")
    page.locator("#t-range").select_option("5y")
    expect(page.locator("#t-chart details")).to_contain_text("periodo 5y")
    assert point_count(page.locator("#t-chart details")) > 1700
    older = [query for query in calls if "before" in query]
    assert len(older) == 1 and older[0]["provider"] == ["mexc"]
    assert "Cobertura limitada" not in page.locator("#t-chart").text_content()

    page.evaluate("location.hash = '#/inicio'")
    page.locator("#home-range").select_option("5y")
    expect(page.locator("#home-market details")).to_contain_text("periodo 5y")
    assert point_count(page.locator("#home-market details")) > 1700
    page.evaluate("location.hash = '#/portfolio'")
    page.locator("#p-tf").select_option("5y")
    expect(page.locator("#p-market details")).to_contain_text("periodo 5y")
    assert point_count(page.locator("#p-market details")) > 1700
    assert len([query for query in calls if "before" in query]) == 1, "El caché breve debe evitar paginar cada refresco"
    page.locator("#p-load").click()
    page.wait_for_function("() => !!document.querySelector('#p-market details') && !document.querySelector('#p-market .loading')")
    assert len([query for query in calls if "before" in query]) == 2, "Cargar explícitamente debe omitir el caché"

    mismatch = browser.new_page(viewport={"width": 1440, "height": 900})
    mismatch.add_init_script("localStorage.setItem('orbis.provider.default', 'binance')")
    mismatch.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "101", "source": "binance", "age_seconds": 1, "stale": False}))
    mismatch.route("**/paper/market/preview?*", mock_preview)

    def wrong_source(route):
        query = parse_qs(urlparse(route.request.url).query)
        older_page = "before" in query
        route.fulfill(status=200, json={"available": True,
                                        "resolved_provider": "kraken" if older_page else "binance",
                                        "older_pagination_supported": True,
                                        "candles": bars((int(query["before"][0]) - DAY) if older_page else TODAY - DAY,
                                                        1000, DAY)})

    mismatch.route("**/market/candles?*", wrong_source)
    mismatch.goto(BASE + "#/operativa")
    mismatch.wait_for_load_state("networkidle")
    mismatch.locator("#t-range").select_option("5y")
    expect(mismatch.locator("#t-chart .amber")).to_contain_text("misma fuente")
    assert point_count(mismatch.locator("#t-chart details")) == 1000
    mismatch.close()
    assert not errors, errors
    browser.close()
    print("Histórico largo: >1700 velas, misma fuente y caché en las tres vistas")
