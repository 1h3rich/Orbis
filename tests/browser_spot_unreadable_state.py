"""Spot PAPER detenido muestra ausencia de cifras sin permitir cierre ficticio."""

import json
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8000"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
SESSION = {
    "id": SESSION_ID, "version_id": "22222222-2222-4222-8222-222222222222",
    "dataset_id": "33333333-3333-4333-8333-333333333333",
    "dataset_hash": "a" * 64, "symbol": "BTC/USDT", "timeframe": "1h",
    "started_at_ns": 1_735_689_600_000_000_000, "last_t": 1_735_689_600,
    "status": "GAP", "status_reason": "Estado PAPER spot incoherente; sesión detenida sin operar",
    "state": None, "last_quote": None, "engine_version": "lab-1.0",
    "execution": "LOCAL_SPOT_PAPER_BID_ASK_APPROXIMATION_NO_REAL_ORDERS",
    "valuation": {"basis": "UNAVAILABLE_INVALID_STATE", "equity_if_closed": None,
                  "unrealized_pnl_if_closed": None},
    "events": [], "coverage": None,
}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 850})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def mock_api(route):
        path = urlsplit(route.request.url).path
        if path in ("/lab/datasets", "/lab/strategy-versions"):
            body = []
        elif path == "/paper/spot/strategies":
            body = [SESSION]
        elif path == "/paper/spot/strategies/worker":
            body = {"running": False}
        elif path == f"/paper/spot/strategies/{SESSION_ID}":
            body = SESSION
        elif path.endswith("/gap-analyses"):
            body = []
        elif path.endswith("/chart"):
            route.fulfill(status=422, content_type="application/json",
                          body=json.dumps({"detail": "Estado PAPER no verificable"}))
            return
        else:
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/lab/datasets", mock_api)
    page.route("**/lab/strategy-versions", mock_api)
    page.route("**/paper/spot/strategies**", mock_api)
    page.goto(BASE + "/ui/#/spot-paper")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#spot-paper-detail .statusbadge")).to_have_text("GAP")
    expect(page.locator("#spot-paper-detail")).to_contain_text("Estado PAPER incoherente")
    expect(page.locator("#spot-paper-detail")).to_contain_text("Comparación no disponible")
    expect(page.locator("#spot-paper-close")).to_be_disabled()
    expect(page.locator("#spot-paper-poll")).to_be_disabled()
    assert "≈" not in page.locator("#spot-paper-detail .spot-paper-metrics").inner_text()
    assert not errors, errors
    browser.close()
