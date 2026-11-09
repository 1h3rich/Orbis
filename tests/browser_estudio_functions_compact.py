"""Comprueba la cuadrícula de funciones Estudio y sus paneles reales."""

import os

from playwright.sync_api import sync_playwright


base = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    errors = []
    for width, height in ((1740, 900), (390, 844)):
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/ui/#/estudio")
        page.wait_for_load_state("networkidle")
        menu = page.locator(".st-func-menu")
        menu.wait_for(state="visible")
        assert menu.locator("button").count() == 6
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        assert page.evaluate("getComputedStyle(document.querySelector('.st-func-menu')).gridTemplateColumns").count("px") == 2
        assert menu.bounding_box()["height"] < 160
        menu.locator('[data-stf="inds"]').click()
        assert page.locator("#stf-panel-inds").is_visible()
        assert menu.locator('[data-stf="inds"]').get_attribute("aria-expanded") == "true"
        menu.locator('[data-stf="alerts"]').click()
        assert page.locator("#stf-panel-alerts").is_visible()
        assert not page.locator("#stf-panel-inds").is_visible()
        menu.locator('[data-stf="alerts"]').click()
        assert not page.locator("#stf-panel-alerts").is_visible()
        page.screenshot(path=f"/tmp/orbis-estudio-functions-{width}.png", full_page=True)
        page.close()
    assert not errors, errors
    browser.close()
    print("Estudio: funciones compactas, paneles reales, sin desbordamiento")
