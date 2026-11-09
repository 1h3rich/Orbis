"""Estado visible de vigilancia MEXC, sin credenciales ni red de exchange."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
state = {
    "running": False, "status": "NOT_STARTED", "stop_requested": False,
    "stalled": False,
    "cycles": 0, "intent_id": None, "unresolved_intents": None,
    "new_orders_enabled": False, "scope": "LOCAL_PROCESS_ONLY",
    "monitoring_active": False,
    "may_cancel_own_open_order": False,
    "stopped_accounts_with_unresolved": 0, "unresolved_stopped_intents": 0,
    "unmonitored_stopped_accounts": 0, "unmonitored_stopped_intents": 0,
    "recovery_required": False,
}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.clock.install()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/real/execution/mexc/stop-supervisor/status",
               lambda route: route.fulfill(status=200, json=state,
                                           headers={"Cache-Control": "no-store"}))
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser supervisor isolated password")
    page.locator("#real-passphrase-confirm").fill("browser supervisor isolated password")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator("#real-new-account").wait_for()
    page.locator('#real-new-account [name="name"]').fill("Principal")
    page.get_by_role("button", name="Crear cuenta").click()
    expect(page.locator("#real-mexc-supervisor strong")).to_have_text("SIN VIGILANCIA")
    assert page.locator("#real-mexc-supervisor-global").is_hidden()

    state.update(stopped_accounts_with_unresolved=1, unresolved_stopped_intents=1,
                 unmonitored_stopped_accounts=1, unmonitored_stopped_intents=1,
                 recovery_required=True)
    page.clock.fast_forward(11_000)
    expect(page.locator("#real-mexc-supervisor strong")).to_have_text("PARADA SIN VIGILANCIA")
    expect(page.locator("#real-mexc-supervisor-global")).to_contain_text("PARADA SIN VIGILANCIA")
    expect(page.locator("#toasts")).to_contain_text("Parada MEXC sin vigilancia")

    state.update(running=True, status="RUNNING", cycles=2,
                 unresolved_intents=1, monitoring_active=True,
                 may_cancel_own_open_order=True,
                 unmonitored_stopped_accounts=0, unmonitored_stopped_intents=0,
                 recovery_required=False)
    page.clock.fast_forward(11_000)
    expect(page.locator("#real-mexc-supervisor strong")).to_have_text("VIGILANCIA LOCAL ACTIVA")
    expect(page.locator("#real-mexc-supervisor-global")).to_contain_text("VIGILANCIA LOCAL ACTIVA")
    page.screenshot(path="/tmp/orbis-mexc-supervisor-status.png", full_page=False)
    page.locator("#real-mexc-supervisor").scroll_into_view_if_needed()
    page.screenshot(path="/tmp/orbis-mexc-supervisor-panel.png", full_page=False)

    # El hilo puede seguir vivo unos instantes tras marcar un estado terminal.
    # El indicador no debe llamarlo vigilancia activa ni pintar verde.
    state.update(status="VAULT_SESSION_REPLACED", monitoring_active=False,
                 recovery_required=True, unmonitored_stopped_accounts=1,
                 unmonitored_stopped_intents=1)
    page.clock.fast_forward(6_000)
    expect(page.locator("#real-mexc-supervisor strong")).to_have_text("PARADA SIN VIGILANCIA")
    expect(page.locator("#real-mexc-supervisor")).to_have_attribute("data-state", "error")
    state.update(status="RUNNING", monitoring_active=True, recovery_required=False,
                 unmonitored_stopped_accounts=0, unmonitored_stopped_intents=0)
    page.clock.fast_forward(6_000)
    expect(page.locator("#real-mexc-supervisor strong")).to_have_text("VIGILANCIA LOCAL ACTIVA")

    state.update(stalled=True, stopped_accounts_with_unresolved=0,
                 unresolved_stopped_intents=0)
    page.clock.fast_forward(6_000)
    expect(page.locator("#real-mexc-supervisor strong")).to_have_text("VIGILANCIA ESTANCADA")
    expect(page.locator("#toasts")).to_contain_text("La vigilancia MEXC está estancada")

    state.update(stalled=False, stop_requested=True, monitoring_active=False,
                 stopped_accounts_with_unresolved=1, unresolved_stopped_intents=1,
                 unmonitored_stopped_accounts=1, unmonitored_stopped_intents=1,
                 recovery_required=True)
    page.clock.fast_forward(6_000)
    expect(page.locator("#real-mexc-supervisor strong")).to_have_text("PARADA SIN VIGILANCIA")

    page.goto(BASE + "/ui/#/inicio")
    page.wait_for_load_state("networkidle")
    state.update(running=False, status="STALLED_NEEDS_REVIEW", stop_requested=False,
                 monitoring_active=False, may_cancel_own_open_order=False,
                 recovery_required=False, unmonitored_stopped_accounts=0,
                 unmonitored_stopped_intents=0)
    page.clock.fast_forward(6_000)
    expect(page.locator("#real-mexc-supervisor-global")).to_contain_text("VIGILANCIA CAÍDA")
    expect(page.locator("#toasts")).to_contain_text("La vigilancia MEXC ha terminado")
    page.locator("#real-mexc-supervisor-global").click()
    expect(page.locator("#real-mexc-supervisor strong")).to_have_text("VIGILANCIA CAÍDA")
    page.locator("#real-lock").click()
    expect(page.locator("#real-mexc-supervisor-global")).to_be_hidden()
    assert not errors, errors
    print("Browser MEXC supervisor status flow passed")
    browser.close()
