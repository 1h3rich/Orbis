"""Lectura sin mutaciones: páginas de evidencias y gráfico de un run LAB existente."""

import os
import json
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


base = os.environ.get("ORBIS_BROWSER_BASE", "http://127.0.0.1:8000")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    lazy_event_calls = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: lazy_event_calls.append(response.url)
            if "/events?offset=" in response.url else None)
    runs = page.request.get(base + "/lab/runs")
    assert runs.ok, runs.text()
    assert runs.json(), "Se requiere un run LAB existente; esta prueba no crea datos"
    run_id = runs.json()[0]["id"]
    details = page.request.get(base + "/lab/runs/" + run_id)
    assert details.ok, details.text()
    run = details.json()

    page.goto(base + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    with page.expect_response(lambda response: "/lab/runs/" + run_id + "?view=chart" in response.url) as chart_response:
        page.locator("#lab-run-select").select_option(run_id)
    chart_run = chart_response.value.json()
    assert chart_run["view_mode"] == "chart"
    assert all("explanation" not in event for event in chart_run["events"])
    assert chart_run["metrics"] == run["metrics"]
    expect(page.locator("#lab-trade-rows tr")).to_have_count(min(50, len(run["trades"])))
    expect(page.locator("#lab-event-rows tr")).to_have_count(min(50, len(run["events"])))
    assert page.locator("#lab-report > details pre.lab-events").count() == 1
    page.locator("#lab-chart canvas").first.wait_for()
    if run["events"]:
        page.locator("#lab-event-rows details").first.locator("summary").click()
        expect(page.locator("#lab-event-rows details").first.locator("pre")).to_contain_text("signal_time")
    if run["trades"]:
        lazy_event_calls.clear()
        page.locator("#lab-trade-rows tr").first.click()
        expect(page.locator("#lab-chart-detail")).to_contain_text("Entrada simulada")
        if chart_run["events"]:
            page.locator("#lab-chart").scroll_into_view_if_needed()
            box = page.locator("#lab-chart").bounding_box()
            for delta in range(350, 850, 15):
                if lazy_event_calls:
                    break
                page.mouse.move(box["x"] + delta, box["y"] + box["height"] / 2)
                page.wait_for_timeout(140)
            assert lazy_event_calls, "El cursor no recuperó la condición exacta del marcador"
            expect(page.locator("#lab-chart-detail")).to_contain_text("Señal al cierre:")
    def many_events(route):
        query = parse_qs(urlparse(route.request.url).query)
        offset = int(query["offset"][0])
        items = [{"signal_time": 1735689600 + i * 3600, "action": "ENTER_NEXT_OPEN",
                  "explanation": {"kind": "CLOSE_ABOVE_EMA", "period": 2,
                                  "observed": "10", "threshold": "9"}}
                 for i in range(offset, min(offset + 50, 121))]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "run_id": run_id, "offset": offset, "limit": 50, "total": 121, "items": items,
        }))

    page.route("**/lab/runs/" + run_id + "/events?*", many_events)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run_id)
    expect(page.locator("#lab-event-rows tr")).to_have_count(50)
    page.locator("#lab-event-next").click()
    expect(page.locator("#lab-event-page")).to_have_text("51–100 / 121")
    assert page.locator("#lab-event-rows tr").first.locator("td").first.inner_text() == "51"
    page.locator("#lab-event-next").click()
    expect(page.locator("#lab-event-rows tr")).to_have_count(21)
    expect(page.locator("#lab-event-next")).to_be_disabled()
    assert not errors, errors
    print("Browser LAB pages passed: rows, exact event, chart focus, no JS errors")
    browser.close()
