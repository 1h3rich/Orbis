"""Los controles de vigilancia exigen confirmación y nunca prometen cancelar."""

import json

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "browser booking vault password"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = []
    calls = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    state = {"running": False}

    def snapshot():
        return {"running": state["running"],
                "status": "RUNNING" if state["running"] else "STOPPED_BY_USER",
                "stop_requested": False, "stalled": False, "cycles": 0,
                "recovery_required": not state["running"],
                "unmonitored_stopped_intents": 0 if state["running"] else 1,
                "unmonitored_stopped_accounts": 0 if state["running"] else 1}

    def status_route(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(snapshot()))

    def start_route(route):
        assert json.loads(route.request.post_data)["confirmation"] == "VIGILAR_PARADA"
        calls.append("start")
        state["running"] = True
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(snapshot()))

    def stop_route(route):
        assert json.loads(route.request.post_data)["confirmation"] == "DETENER_VIGILANCIA"
        calls.append("stop")
        state["running"] = False
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(snapshot()))

    page.route("**/real/execution/mexc/stop-supervisor/status", status_route)
    page.route("**/real/execution/mexc/intents/*/supervise-stop", start_route)
    page.route("**/real/execution/mexc/stop-supervisor/stop", stop_route)
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-mexc-execution summary").click()
    local_stop = page.locator("#real-mexc-execution-list .real-exec-stop").first
    local_stop.locator("summary").click()
    local_stop.locator('input[name="confirmation"]').fill("DETENER")
    with page.expect_response("**/real/execution/mexc/intents/*/stop-account") as stopped:
        local_stop.get_by_role("button", name="Detener", exact=True).click()
    assert stopped.value.status == 200

    supervise = page.locator("#real-mexc-execution-list .real-exec-supervise")
    expect(supervise).to_be_visible()
    supervise.locator("summary").click()
    expect(supervise).to_contain_text("No es una orden protectora")
    confirm = supervise.locator('input[name="confirmation"]')
    confirm.fill("VIGILAR")
    supervise.get_by_role("button", name="Iniciar vigilancia").click()
    assert calls == []
    confirm.fill("VIGILAR_PARADA")
    supervise.get_by_role("button", name="Iniciar vigilancia").click()
    expect(page.locator("#real-mexc-supervisor")).to_contain_text("VIGILANCIA LOCAL ACTIVA")
    assert calls == ["start"]
    expect(supervise).to_be_hidden()

    stop = page.locator("#real-mexc-supervisor-stop")
    expect(stop).to_be_visible()
    stop.locator("summary").click()
    stop.locator('input[name="confirmation"]').fill("DETENER")
    stop.get_by_role("button", name="Detener vigilancia").click()
    assert calls == ["start"]
    stop.locator('input[name="confirmation"]').fill("DETENER_VIGILANCIA")
    stop.get_by_role("button", name="Detener vigilancia").click()
    expect(page.locator("#real-mexc-supervisor")).to_contain_text("PARADA SIN VIGILANCIA")
    assert calls == ["start", "stop"]
    expect(stop).to_be_hidden()
    expect(supervise).to_be_visible()
    assert not errors, errors
    browser.close()
