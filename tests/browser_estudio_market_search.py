"""Buscador accionable del Estudio; ejecutar contra servidor local."""

import os

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "**/market/symbols?*",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"providers":{"binance":["BTC/USDT","ETH/USDT"],"kraken":["BTC/EUR"]}}',
        ),
    )
    page.goto(BASE_URL + "/ui/#/estudio")
    page.wait_for_load_state("networkidle")
    pair = page.locator("#st-pair")
    pair.fill("BTC")
    choices = page.locator("#st-search button[data-market]")
    choices.first.wait_for()
    assert choices.count() == 3
    assert pair.get_attribute("aria-expanded") == "true"
    pair.press("ArrowDown")
    assert choices.first.evaluate("el => el === document.activeElement")
    assert "BTC/EUR" in page.locator("#st-meta").inner_text()
    choices.first.press("Enter")
    assert pair.input_value() == "BTC/USDT"
    assert page.locator("#st-prov").input_value() == "binance"
    assert page.locator("#st-search").is_hidden()
    pair.fill("BTC")
    choices.first.wait_for()
    pair.press("Escape")
    assert page.locator("#st-search").is_hidden()
    assert pair.get_attribute("aria-expanded") == "false"
    assert not errors, errors
    browser.close()
    print("Estudio: búsqueda por teclado selecciona mercado y fuente; Escape cierra")
