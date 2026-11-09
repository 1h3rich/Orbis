"""Smoke visual y funcional de la barra compacta del Estudio.

Ejecutar contra un servidor local: ORBIS_TEST_BASE_URL=http://127.0.0.1:8000
.venv/bin/python tests/browser_estudio_toolbar.py
"""

import os

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    errors = []
    desktop = browser.new_page(viewport={"width": 1740, "height": 900})
    desktop.on("pageerror", lambda error: errors.append(str(error)))
    desktop.goto(BASE_URL + "/ui/#/estudio")
    desktop.wait_for_load_state("networkidle")
    chart = desktop.locator("#st-chart")
    chart.wait_for(state="visible")
    assert chart.bounding_box()["height"] >= 600
    assert desktop.locator("#st-range").is_visible()
    assert desktop.locator("#st-range option[value='current']").inner_text() == "Últimas velas"
    assert not desktop.locator(".selector-fuente").is_visible()
    desktop.locator("#st-prov").select_option("bitstamp")
    assert desktop.evaluate("localStorage.getItem('orbis.provider.default')") is None
    assert desktop.locator("#provider-sel").input_value() == "auto"
    assert desktop.evaluate("JSON.parse(localStorage.getItem('orbis.estudio.cfg|all|x|provider'))") == "bitstamp"
    desktop.evaluate("window.OrbisUI.setOrbisProvider('kraken')")
    assert desktop.locator("#st-prov").input_value() == "kraken"
    desktop.locator('nav a[href="#/inicio"]').click()
    desktop.locator('body[data-view="inicio"]').wait_for()
    assert desktop.locator(".selector-fuente").is_visible()
    assert desktop.locator("#provider-sel").input_value() == "kraken"
    desktop.locator('nav a[href="#/estudio"]').click()
    desktop.locator('body[data-view="estudio"]').wait_for()
    assert desktop.locator("#st-prov").input_value() == "kraken"
    view = desktop.locator(".st-view-actions")
    assert not view.get_attribute("open")
    for button_id in ("st-zin", "st-zout", "st-reset"):
        view.locator("summary").click()
        assert desktop.locator("#" + button_id).is_visible()
        desktop.locator("#" + button_id).click()
        assert not view.get_attribute("open")
    desktop.screenshot(path="/tmp/orbis-estudio-toolbar-desktop.png", full_page=True)

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile.on("pageerror", lambda error: errors.append(str(error)))
    mobile.goto(BASE_URL + "/ui/#/estudio")
    mobile.wait_for_load_state("networkidle")
    assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
    assert mobile.locator("#st-chart").is_visible()
    assert mobile.locator("#st-range").is_visible()
    assert not mobile.locator(".selector-fuente").is_visible()
    mobile.locator(".st-view-actions summary").click()
    assert mobile.locator("#st-reset").is_visible()
    mobile.screenshot(path="/tmp/orbis-estudio-toolbar-mobile.png", full_page=True)
    assert not errors, errors
    browser.close()
    print("Estudio: gráfica alta, rango visible y menú Vista funcional en escritorio/móvil")
