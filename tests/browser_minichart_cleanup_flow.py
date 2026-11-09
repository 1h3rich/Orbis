"""Regresión: destruir mini-gráficos durante navegación rápida no deja frames colgados.

Requiere un servidor local ORBIS_TEST_BASE_URL (por defecto :8000).
"""

import os

from playwright.sync_api import sync_playwright

from browser_paper_preview_mock import mock_preview


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
POINTS = [
    {"t": 1_800_000_000 + hour * 3600, "open": str(100 + hour),
     "high": str(102 + hour), "low": str(99 + hour), "close": str(101 + hour)}
    for hour in range(24)
]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error) + "\n" + str(getattr(error, "stack", ""))))
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "123", "source": "kraken", "age_seconds": 1, "stale": False}))
    page.route("**/paper/market/preview?*", mock_preview)
    page.route("**/market/history?*", lambda route: route.fulfill(status=200, json={
        "points": POINTS, "source": "kraken", "timeframe": "24h"}))
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "kraken", "candles": []}))
    for index in range(12):
        page.goto(BASE + "/ui/#/inicio" if index % 2 == 0 else BASE + "/ui/#/operativa")
        page.wait_for_load_state("networkidle")
        chart_id = "home-chart-cv" if index % 2 == 0 else "t-chart-cv"
        page.locator("#" + chart_id + " canvas").first.wait_for(state="visible")
        page.set_viewport_size({"width": 425 if index % 3 == 0 else 1440, "height": 900})
    page.wait_for_timeout(120)
    assert not errors, errors
    browser.close()
    print("Mini-gráficos: 12 navegaciones/redimensionados sin frames sobre canvas destruido")
