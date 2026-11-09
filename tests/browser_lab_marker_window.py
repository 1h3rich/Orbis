"""Un run largo conserva todas las marcas al cambiar el rango sin dibujarlas todas a la vez.

Sólo sustituye la respuesta del gráfico en Chromium; no cambia datos LAB locales.
"""

import json
import os
import re
from urllib.parse import parse_qs, urlparse

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
    run = run_response.json()
    dataset_response = page.request.get(base + "/lab/datasets/" + run["dataset_id"])
    assert dataset_response.ok
    candles = dataset_response.json()["candles"]
    assert len(candles) > 1500
    events = [{"index": index, "signal_time": candle["t"],
               "action": "ENTER_NEXT_OPEN"}
              for index, candle in enumerate(candles)]
    trades = run["trades"]
    total_markers = len(events) + 2 * len(trades)
    shell = {**run, "events": [], "trades": [], "signal_count": len(events),
             "trade_count": len(trades), "markers_windowed": True}
    marker_calls = []
    fail_once = {"value": False}

    def markers(route):
        query = parse_qs(urlparse(route.request.url).query)
        from_t, to_t = int(query["from_t"][0]), int(query["to_t"][0])
        marker_calls.append((from_t, to_t))
        if fail_once["value"]:
            fail_once["value"] = False
            route.fulfill(status=503, content_type="application/json",
                          body='{"detail":"Prueba de red"}')
            return
        window = {"run_id": run_id, "from_t": from_t, "to_t": to_t,
                  "events": [event for event in events if from_t <= event["signal_time"] <= to_t],
                  "trades": [trade for trade in trades if
                             from_t <= trade["entry_time"] <= to_t or
                             from_t <= trade["exit_time"] <= to_t]}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(window))

    page.route("**/lab/runs/" + run_id + "?view=chart&markers=window&curves=window",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=json.dumps(shell)))
    page.route("**/lab/runs/" + run_id + "/markers?*", markers)
    page.goto(base + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run_id)
    chart = page.locator("#lab-chart")
    expect(chart).to_have_attribute("data-signal-count", str(len(candles)))
    expect(chart).to_have_attribute("data-entry-count", str(len(run["trades"])))
    expect(page.locator("#lab-run-history")).to_have_attribute("data-view", "1")
    expect(chart).not_to_have_attribute("data-marker-visible-count", str(total_markers))
    expect(chart).to_have_attribute("data-marker-visible-count", re.compile(r"^[1-9][0-9]*$"))
    first_count = int(chart.get_attribute("data-marker-visible-count"))
    assert 0 < first_count < total_markers
    page.locator("#lab-run-history select").select_option("all")
    expect(chart).to_have_attribute("data-marker-visible-count", str(total_markers))
    page.locator("#lab-run-history select").select_option("1")
    expect(chart).not_to_have_attribute("data-marker-visible-count", str(total_markers))
    assert len(marker_calls) >= 2
    fail_once["value"] = True
    page.locator("#lab-run-history select").select_option("5")
    expect(page.locator("#lab-marker-status")).to_contain_text("No se pudieron cargar")
    page.get_by_role("button", name="Reintentar", exact=True).click()
    expect(page.locator("#lab-marker-status")).to_be_hidden()
    assert not fail_once["value"]
    assert not errors, errors
    browser.close()
