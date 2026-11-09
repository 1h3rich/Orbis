"""Búsqueda global local: destinos reales, teclado y ancho móvil; sin escrituras."""

import os

from playwright.sync_api import expect, sync_playwright


base = os.environ.get("ORBIS_BROWSER_BASE", "http://127.0.0.1:8000")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    for width in (1440, 390):
        page = browser.new_page(viewport={"width": width, "height": 850})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/ui/#/inicio")
        page.wait_for_load_state("networkidle")
        page.keyboard.press("Control+k")
        dialog = page.locator("#global-search-dialog")
        expect(dialog).to_be_visible()
        search = page.locator("#global-search-input")
        expect(search).to_be_focused()
        if width == 1440:
            page.screenshot(path="/tmp/orbis-global-search.png")
        search.fill("btc/usd")
        expect(page.locator(".global-search-result")).to_have_count(1)
        page.keyboard.press("Enter")
        expect(dialog).to_be_hidden()
        expect(page).to_have_url(base + "/ui/#/estudio?market=BTC%2FUSD")
        expect(page.locator("#st-pair")).to_have_value("BTC/USD")

        page.locator("#global-search-open").click()
        search.fill("nueva estrategia")
        expect(page.locator(".global-search-result")).to_have_count(1)
        page.locator(".global-search-result").click()
        expect(page).to_have_url(base + "/ui/#/laboratorio")
        expect(page.locator("#lab-strategy-form [name='name']")).to_be_focused()

        page.keyboard.press("Control+k")
        search.fill("sin-existencia")
        expect(page.locator(".global-search-empty")).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        page.keyboard.press("Control+k")
        search.fill("registrar compra")
        page.keyboard.press("Enter")
        expect(page).to_have_url(base + "/ui/#/real")
        expect(page.locator("#real-auth-form")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors, errors
        page.close()
    browser.close()
