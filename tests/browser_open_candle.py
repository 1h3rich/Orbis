"""Inicio y Operativa identifican velas en formación sin inventar cierres."""

import os
import time

from playwright.sync_api import expect, sync_playwright
from browser_paper_preview_mock import mock_preview


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000/ui/")
DAY = 86400
TODAY = int(time.time()) // DAY * DAY
mode = {"open": True}


def candles(route):
    rows = [{"t": TODAY - (8 - index) * DAY, "o": "100", "h": "105",
             "l": "95", "c": str(100 + index), "v": "10",
             "closed": not mode["open"] if index == 8 else True}
            for index in range(9)]
    route.fulfill(status=200, json={"available": True, "resolved_provider": "kraken",
                                    "candles": rows})


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("localStorage.setItem('orbis.provider.default', 'kraken')")
    page.route("**/market/candles?*", candles)
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "108", "source": "kraken", "age_seconds": 1, "stale": False}))
    page.route("**/paper/market/preview?*", mock_preview)
    page.goto(BASE + "#/inicio")
    page.wait_for_load_state("networkidle")
    page.locator("#home-range").select_option("1y")
    expect(page.locator('[data-chart-range="home-chart-cv"]')).to_have_attribute("data-open-candle", "true")
    expect(page.locator('[data-chart-range="home-chart-cv"]')).to_contain_text("Vela abierta")
    page.goto(BASE + "#/operativa")
    page.wait_for_load_state("networkidle")
    page.locator("#t-range").select_option("1y")
    expect(page.locator('[data-chart-range="t-chart-cv"]')).to_have_attribute("data-open-candle", "true")
    mode["open"] = False
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator('[data-chart-range="t-chart-cv"]')).to_have_attribute("data-open-candle", "false")
    expect(page.locator('[data-chart-range="t-chart-cv"]')).not_to_contain_text("Vela abierta")
    assert not errors, errors
    browser.close()
    print("Inicio/Operativa: vela abierta visible, cierre confirmado sin etiqueta")
