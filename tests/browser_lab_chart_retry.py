"""Un fallo transitorio del histórico LAB se recupera sin recrear el backtest.

Usa un run local existente y sólo intercepta su descarga de velas en Chromium.
"""

import os

from playwright.sync_api import expect, sync_playwright


base = os.environ.get("ORBIS_BROWSER_BASE", "http://127.0.0.1:8000")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    runs_response = page.request.get(base + "/lab/runs")
    assert runs_response.ok and runs_response.json(), "Se requiere un run LAB existente"
    run_id = runs_response.json()[0]["id"]
    run_response = page.request.get(base + "/lab/runs/" + run_id + "?view=chart")
    assert run_response.ok
    dataset_id = run_response.json()["dataset_id"]
    dataset_path = "**/lab/datasets/" + dataset_id + "?view=chart"
    calls = {"count": 0}

    def dataset(route):
        calls["count"] += 1
        if calls["count"] == 1:
            route.fulfill(status=503, content_type="application/json",
                          body='{"detail":"Fallo transitorio de prueba"}')
        else:
            route.continue_()

    page.goto(base + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.route(dataset_path, dataset)
    page.locator("#lab-run-select").select_option(run_id)
    status = page.locator("#lab-chart-load-status")
    expect(status).to_contain_text("No se pudo cargar el histórico")
    expect(page.locator("#lab-chart canvas")).to_have_count(0)
    status.get_by_role("button", name="Reintentar gráfico").click()
    expect(status).to_be_hidden()
    expect(page.locator("#lab-chart canvas").first).to_be_visible()
    assert calls["count"] == 2
    assert not errors, errors
    browser.close()
