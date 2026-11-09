"""La subnavegación PAPER sólo existe visualmente dentro de PAPER."""

import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_BROWSER_BASE", "http://127.0.0.1:8000")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    for width in (1440, 390):
        page = browser.new_page(viewport={"width": width, "height": 850})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE + "/ui/#/inicio")
        page.wait_for_load_state("networkidle")
        primary = page.locator(".navegacion-principal")
        simulation = page.locator("#sim-nav")
        expect(primary.get_by_role("link", name="Simulación PAPER")).to_be_visible()
        expect(primary.get_by_role("link", name="Patrimonio registrado")).to_be_visible()
        expect(simulation).to_be_hidden()

        primary.get_by_role("link", name="Simulación PAPER").click()
        expect(page).to_have_url(BASE + "/ui/#/operativa")
        expect(simulation).to_be_visible()
        expect(primary.get_by_role("link", name="Simulación PAPER")).to_have_attribute("aria-current", "page")
        assert simulation.locator('a[href="#/operativa"]').count() == 0
        simulation.get_by_role("link", name="Saldo y posiciones").click()
        expect(page).to_have_url(BASE + "/ui/#/portfolio")
        expect(simulation).to_be_visible()

        primary.get_by_role("link", name="Patrimonio registrado").click()
        expect(page).to_have_url(BASE + "/ui/#/real")
        expect(simulation).to_be_hidden()
        expect(page.locator("#real-auth-form")).to_be_visible()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), width
        assert not errors, errors
        page.close()
    browser.close()
