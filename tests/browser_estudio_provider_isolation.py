"""Cambiar la fuente del Estudio no altera la fuente de Inicio/Operativa."""

import os

from playwright.sync_api import expect, sync_playwright

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
    history_calls = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "123", "source": "kraken", "age_seconds": 1, "stale": False}))
    page.route("**/paper/market/preview?*", mock_preview)

    def history(route):
        history_calls.append(route.request.url)
        route.fulfill(status=200, json={"points": POINTS, "source": "kraken", "timeframe": "24h"})

    page.route("**/market/history?*", history)
    page.goto(BASE + "/ui/#/estudio")
    page.wait_for_load_state("networkidle")
    assert page.locator("#provider-sel").input_value() == "auto"
    page.locator("#st-prov").select_option("bitstamp")
    expect(page.locator("#st-prov")).to_have_value("bitstamp")
    expect(page.locator("#modeline")).to_contain_text("Fuente del Estudio BITSTAMP")
    assert page.locator("#provider-sel").input_value() == "auto"
    assert page.evaluate("localStorage.getItem('orbis.provider.default')") is None
    assert page.evaluate("JSON.parse(localStorage.getItem('orbis.estudio.cfg|all|x|provider'))") == "bitstamp"

    page.locator('.navegacion-principal a[href="#/operativa"]').click()
    page.locator("#t-chart-cv canvas").first.wait_for(state="visible")
    assert any("timeframe=24h" in call for call in history_calls), history_calls
    assert page.locator("#provider-sel").input_value() == "auto"
    page.locator('.navegacion-principal a[href="#/estudio"]').click()
    expect(page.locator("#st-prov")).to_have_value("bitstamp")
    assert not errors, errors
    browser.close()
    print("Estudio Bitstamp aislado; Operativa AUTO conserva histórico 24h")
