"""DCA PAPER muestra una sesión detenida sin inventar el saldo corrupto."""

import json
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8000"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
SESSION = {
    "id": SESSION_ID, "version_id": "22222222-2222-4222-8222-222222222222",
    "dataset_id": "33333333-3333-4333-8333-333333333333",
    "dataset_hash": "a" * 64, "symbol": "BTC/USDT", "source": "mexc-spot-public",
    "next_exec_t": 1_735_948_800, "tick_index": 0, "started_at_ns": 1_735_689_600_000_000_000,
    "status": "GAP", "status_reason": "Estado DCA no verificable; sesión detenida",
    "state": None, "last_quote": None, "engine_version": "dca-1.6",
    "reference_mode": "initial", "mode": "PAPER_DCA_MEXC_SPOT_APPROXIMATION",
    "execution": "NO_EXCHANGE_ORDERS", "events": [],
}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 850})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def mock_api(route):
        path = urlsplit(route.request.url).path
        if path in ("/lab/datasets", "/automation/dca/rules", "/automation/dca/runs"):
            body = []
        elif path == "/automation/dca/paper/sessions":
            body = [SESSION]
        elif path == "/automation/dca/paper/worker":
            body = {"running": False, "heartbeat_age_seconds": None}
        elif path == f"/automation/dca/paper/sessions/{SESSION_ID}":
            body = SESSION
        elif path.endswith("/gap-analyses"):
            body = []
        elif path.endswith("/comparison"):
            body = {"historical": None, "paper": {"observed_days": 0, "paper_buys": 0},
                    "comparison_basis": "NOT_DIRECTLY_COMPARABLE"}
        elif path.endswith("/chart"):
            route.fulfill(status=422, content_type="application/json",
                          body=json.dumps({"detail": "Estado DCA no verificable"}))
            return
        else:
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/lab/datasets", mock_api)
    page.route("**/automation/dca/**", mock_api)
    page.goto(BASE + "/ui/#/dca")
    page.wait_for_load_state("networkidle")
    page.locator("#dca-paper-session").select_option(SESSION_ID)
    expect(page.locator("#dca-paper-report .statusbadge")).to_have_text("GAP")
    expect(page.locator("#dca-paper-report")).to_contain_text("Saldo y unidades · no disponibles")
    expect(page.locator("#dca-paper-report")).to_contain_text("Estado DCA no verificable")
    expect(page.locator("#dca-paper-poll")).to_be_disabled()
    assert not errors, errors
    browser.close()
