"""Smoke de Estudio: archivo real BTC 2012 y rangos multianuales."""

import re

from playwright.sync_api import expect, sync_playwright
import os

base_url = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    phase = ["inicio"]
    page.on("pageerror", lambda error: errors.append(phase[0] + ": " + str(error) + "\n" + str(getattr(error, "stack", ""))))
    page.goto(base_url + "/ui/#/estudio")
    page.wait_for_load_state("networkidle")
    page.locator("#st-pair").fill("BTC/USD")
    page.locator("#st-prov").select_option("auto")
    page.locator("#st-range").select_option("all")
    page.get_by_text("Archivo 2012-01-01", exact=False).wait_for(timeout=30000)
    assert page.locator("#st-pair").input_value() == "BTC/USD"
    assert page.locator("#st-prov").input_value() == "auto"
    assert page.locator("#st-tf").input_value() == "1d"
    assert "BTC/USD" in page.locator("#st-archive-meta").inner_text()
    page.locator(".st-view-actions summary").click()
    year = page.locator("#st-archive-year")
    expect(year).to_be_visible(timeout=10000)
    assert year.input_value() == ""
    assert page.locator("#st-chart").get_attribute("data-archive-window") == "all"
    expect(page.locator("#st-chart")).to_have_attribute("data-archive-from", "1325376000")
    # En una ventana estrecha el límite de separación entre velas no debe
    # convertir "Todo disponible" en un recorte silencioso del histórico.
    phase[0] = "estrechar"
    page.set_viewport_size({"width": 425, "height": 900})
    expect(page.locator("#st-chart")).to_have_attribute("data-archive-from", "1325376000")
    phase[0] = "ensanchar"
    page.set_viewport_size({"width": 1440, "height": 900})
    page.screenshot(path="/tmp/orbis-estudio-rango-compacto.png", full_page=True)
    phase[0] = "rango 10"
    page.locator("#st-range").select_option("10")
    page.get_by_text("Archivo 2012-01-01", exact=False).wait_for(timeout=30000)
    assert page.locator("#st-chart").get_attribute("data-archive-window") == "10"
    phase[0] = "rango 5"
    page.locator("#st-range").select_option("5")
    page.get_by_text("Archivo 2012-01-01", exact=False).wait_for(timeout=30000)
    assert page.locator("#st-chart").get_attribute("data-archive-window") == "5"
    phase[0] = "año 2012"
    year.select_option("2012")
    assert page.locator("#st-chart").get_attribute("data-archive-window") == "year:2012"
    phase[0] = "año 2024"
    year.select_option("2024")
    assert page.locator("#st-chart").get_attribute("data-archive-window") == "year:2024"
    expect(page.locator("#st-chart")).to_have_attribute("data-archive-from", re.compile(r"17[0-3][0-9]{7}"))
    visible_from = int(page.locator("#st-chart").get_attribute("data-archive-from"))
    visible_to = int(page.locator("#st-chart").get_attribute("data-archive-to"))
    assert 1704067200 <= visible_from < visible_to < 1735689600, (visible_from, visible_to)
    assert "sin actualización automática" in page.locator("#st-refresh-note").inner_text()
    assert page.locator("#st-chart canvas").count() > 0
    phase[0] = "rango actual"
    page.locator("#st-range").select_option("current")
    assert page.locator("#st-archive-year-wrap").is_hidden()
    assert page.locator("#st-chart").get_attribute("data-archive-window") is None
    phase[0] = "cambio mercado"
    page.locator("#st-pair").fill("BTC/EUR")
    page.locator("#st-range").select_option("all")
    expect(page.locator("#st-archive-meta")).to_contain_text("Archivo 2016-04-16", timeout=30000)
    expect(page.locator("#st-archive-meta")).to_contain_text("BTC/EUR")
    assert page.locator("#st-tf").input_value() == "1d"
    assert page.locator("#st-chart").get_attribute("data-archive-window") == "all"
    phase[0] = "navegar Inicio"
    page.goto(base_url + "/ui/#/inicio")
    page.locator("#home-range").select_option("all")
    expect(page.locator('[data-chart-range="home-chart-cv"]')).to_contain_text("2016", timeout=30000)
    phase[0] = "navegar Operativa"
    page.goto(base_url + "/ui/#/operativa")
    page.locator("#t-range").select_option("all")
    expect(page.locator('[data-chart-range="t-chart-cv"]')).to_contain_text("2016", timeout=30000)
    assert not errors, errors
    print("BTC archive browser flow passed: 2012 coverage, source, 1/5/10 years and all")
    browser.close()
