"""UI de sensibilidad LAB con respuestas simuladas; no altera datos del usuario."""

import json
import os

from playwright.sync_api import expect, sync_playwright


base = os.environ.get("ORBIS_BROWSER_BASE", "http://127.0.0.1:8000")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    existing = page.request.get(base + "/lab/runs")
    assert existing.ok and existing.json(), "Se requiere un run LAB existente para probar la interfaz"
    summary = existing.json()[0]
    run_id = summary["id"]
    response = page.request.get(base + f"/lab/runs/{run_id}?view=chart&markers=window&curves=window")
    assert response.ok, response.text()
    run = response.json()
    # El servidor local puede tener un run pequeño y una versión antigua: sólo
    # la respuesta del navegador se adapta; no se guarda ni modifica el run.
    run["metrics"]["trades"] = 30
    run["trades"] = run["trades"] * 3
    summary["metrics"]["trades"] = 30

    def fulfill(route, payload, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    page.route("**/lab/runs", lambda route: fulfill(route, [summary]))
    page.route("**/lab/runs/" + run_id + "?view=chart&markers=window&curves=window",
               lambda route: fulfill(route, run))
    calls = []

    def report(route):
        calls.append(route.request.url)
        if len(calls) == 1:
            fulfill(route, {"detail": "Not Found"}, 404)
        elif len(calls) == 2:
            fulfill(route, {"detail": "El backtest no es reproducible"}, 409)
        else:
            fulfill(route, {"verified": True, "trades": 30, "paths": 100,
                            "ruin_paths": 0, "observed_closed_trade_drawdown_pct": "12.5",
                            "drawdown_pct": {"median": "15", "p95": "31", "worst": "40"}})

    page.route("**/lab/runs/" + run_id + "/sequence-risk?paths=*", report)
    page.goto(base + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run_id)
    panel = page.locator(".lab-sequence-panel")
    expect(panel).to_contain_text("Sensibilidad al orden de operaciones")
    panel.locator("summary").click()
    expect(panel).to_contain_text("no es un pronóstico")
    panel.locator("#lab-sequence-paths").select_option("100")
    panel.locator("#lab-sequence-action").click()
    expect(panel.locator("#lab-sequence-result")).to_contain_text("Reinicia Orbis")
    panel.locator("#lab-sequence-action").click()
    expect(panel.locator("#lab-sequence-result")).to_contain_text("no es reproducible")
    assert not panel.locator("#lab-sequence-action").is_disabled()
    panel.locator("#lab-sequence-action").click()
    expect(panel.locator("#lab-sequence-result")).to_contain_text("Peor reordenación")
    expect(panel.locator("#lab-sequence-result")).to_contain_text("30 cierres · 100 reordenaciones")
    assert len(calls) == 3 and all("paths=100" in url for url in calls)
    assert not errors, errors
    browser.close()
    print("Browser LAB sequence risk passed: failure, retry, theoretical report")
