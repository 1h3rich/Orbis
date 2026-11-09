"""Velas LAB por periodo, marcas y recuperación ante una página fallida.

Requiere browser_lab_marker_window_server.py en :8798; no toca datos del usuario.
"""

import re
import os
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8798")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    if os.environ.get("ORBIS_TEST_LAB_WINDOW"):
        assert page.request.post(BASE+"/lab/unlock",data={"passphrase":"Fixture LAB range 22"}).ok
    runs = page.request.get(BASE + "/lab/runs")
    assert runs.ok and len(runs.json()) == 1
    run_id = runs.json()[0]["id"]
    run = page.request.get(BASE + "/lab/runs/" + run_id + "?view=chart").json()
    dataset_id = run["dataset_id"]
    overview = page.request.get(BASE + "/lab/datasets/" + dataset_id + "?view=chart")
    assert overview.ok
    assert overview.json()["candles_windowed"] is True
    assert overview.json()["candles"] == [] and len(overview.json()["times"]) == 2_200

    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    fail_once = {"first": True, "full": True}
    calls = []

    def candles(route):
        calls.append(route.request.url)
        params = parse_qs(urlparse(route.request.url).query)
        assert params["view"] == ["plot"] and "after_index" not in params
        if fail_once["first"]:
            fail_once["first"] = False
            route.fulfill(status=503, content_type="application/json",
                          body='{"detail":"Fallo inicial de prueba"}')
        elif fail_once["full"] and int(params["limit"][0]) == 2200:
            fail_once["full"] = False
            response = route.fetch()
            malformed = response.json()
            malformed["candles"][100]["t"] = malformed["candles"][99]["t"]
            route.fulfill(response=response,json=malformed)
        else:
            route.continue_()

    page.route("**/lab/datasets/" + dataset_id + "/candles?*", candles)
    page.locator("#lab-run-select").select_option(run_id)
    status = page.locator("#lab-candle-status")
    chart = page.locator("#lab-chart")
    expect(status).to_contain_text("No se pudieron cargar las velas")
    expect(chart).to_have_attribute("data-candle-visible-count", "0")
    assert chart.evaluate("element => getComputedStyle(element).visibility") == "hidden"
    status.get_by_role("button", name="Reintentar velas").click()
    expect(status).to_be_hidden()
    expect(chart).to_have_attribute("data-candle-visible-count", re.compile(r"^[1-9][0-9]*$"))
    first_count = int(chart.get_attribute("data-candle-visible-count"))
    assert first_count < 2_200
    expect(chart).to_have_attribute("data-marker-visible-count", re.compile(r"^[1-9][0-9]*$"))

    # Una operación de la primera página debe llevar al tramo histórico
    # correcto, recuperar sus velas y mantener visible la marca de entrada.
    page.locator("#lab-trade-rows tr").first.click()
    expect(page.locator("#lab-chart-detail")).to_contain_text("Entrada simulada")
    expect(status).to_be_hidden()
    expect(chart).to_have_attribute("data-candle-visible-count", re.compile(r"^[1-9][0-9]*$"))
    expect(chart).to_have_attribute("data-marker-visible-count", re.compile(r"^[1-9][0-9]*$"))

    page.locator("#lab-run-history select").select_option("all")
    expect(status).to_contain_text("No se pudieron cargar las velas")
    expect(chart).to_have_attribute("data-candle-visible-count", "0")
    status.get_by_role("button", name="Reintentar velas").click()
    expect(status).to_be_hidden()
    expect(chart).to_have_attribute("data-candle-visible-count", "2200")
    assert chart.evaluate("element => getComputedStyle(element).visibility") == "visible"
    expect(chart).to_have_attribute("data-marker-visible-count", re.compile(r"^[1-9][0-9]*$"))
    page.locator("#lab-run-history select").select_option("1")
    expect(chart).to_have_attribute("data-candle-visible-count", str(first_count))
    assert len(calls) >= 4 and not any(errors), errors

    page.route("**/lab/datasets/" + dataset_id + "?view=chart",
               lambda route: route.fulfill(json={**overview.json(), "hash": "0" * 64}))
    page.locator("#lab-run-select").dispatch_event("change")
    expect(page.locator("#lab-notice")).to_contain_text("La huella del dataset no coincide")
    assert "gráfico bloqueado" in chart.inner_text()
    expect(page.locator("#lab-run-history")).to_be_hidden()
    expect(page.locator("#lab-equity-range")).to_be_hidden()
    assert not errors, errors
    browser.close()
