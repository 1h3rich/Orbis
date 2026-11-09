"""Curvas LAB exactas por tramo, error recuperable y rango completo voluntario.

Requiere el servidor aislado browser_lab_marker_window_server.py en :8798.
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
    shell_response = page.request.get(
        BASE + "/lab/runs/" + run_id + "?view=chart&markers=window&curves=window")
    assert shell_response.ok
    shell = shell_response.json()
    assert shell["curves_windowed"] is True
    assert shell["equity"] == shell["drawdown"] == []
    assert shell["curve_point_count"] == 2_200

    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    fail_once = {"value": True, "full": True}
    calls = []

    def curves(route):
        calls.append(route.request.url)
        params = parse_qs(urlparse(route.request.url).query)
        assert params["view"] == ["plot"] and "after_index" not in params
        if fail_once["value"]:
            fail_once["value"] = False
            route.fulfill(status=503, content_type="application/json",
                          body='{"detail":"Fallo transitorio de prueba"}')
        elif fail_once["full"] and int(params["limit"][0]) == 2200:
            fail_once["full"] = False
            response = route.fetch()
            malformed = response.json()
            malformed["equity"][100]["t"] += 3600
            malformed["drawdown"][100]["t"] += 3600
            route.fulfill(response=response,json=malformed)
        else:
            route.continue_()

    page.route("**/lab/runs/" + run_id + "/curves?*", curves)
    page.locator("#lab-run-select").select_option(run_id)
    status = page.locator("#lab-curve-status")
    expect(status).to_contain_text("No se pudo cargar la curva")
    status.get_by_role("button", name="Reintentar curva").click()
    expect(status).to_be_hidden()
    chart = page.locator("#lab-equity-chart")
    expect(chart).to_have_attribute("data-curve-visible-count", re.compile(r"^[1-9][0-9]*$"))
    first_count = int(chart.get_attribute("data-curve-visible-count"))
    assert first_count < 2_200
    page.locator("#lab-equity-range select").select_option("all")
    expect(status).to_contain_text("No se pudo cargar la curva")
    expect(chart).to_have_attribute("data-curve-visible-count", "0")
    assert chart.evaluate("element => getComputedStyle(element).visibility") == "hidden"
    status.get_by_role("button", name="Reintentar curva").click()
    expect(status).to_be_hidden()
    expect(chart).to_have_attribute("data-curve-visible-count", "2200")
    assert chart.evaluate("element => getComputedStyle(element).visibility") == "visible"
    page.locator("#lab-equity-range select").select_option("1")
    expect(chart).to_have_attribute("data-curve-visible-count", str(first_count))
    assert len(calls) >= 4
    assert not errors, errors
    browser.close()
