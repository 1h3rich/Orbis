"""Cartera REAL diferencia acuse, orden abierta y final tras cancelar."""

from copy import deepcopy
import json

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "browser booking vault password"
CASES = (
    ("ATTEMPT_OUTCOME_UNKNOWN", "Cancelación incierta · verificar", "ORDER_OBSERVED_OPEN"),
    ("ACKNOWLEDGED_NEEDS_REOBSERVATION", "Cancelación acusada · verificar", "ORDER_OBSERVED_OPEN"),
    ("OPEN_OBSERVED_AFTER_CANCEL", "Sigue abierta tras cancelar", "ORDER_OBSERVED_OPEN"),
    ("FINAL_OBSERVED_AFTER_CANCEL", "Final tras cancelar · reserva pendiente", "FINAL_NO_EXECUTION"),
)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def mock_execution(route):
        response = route.fetch()
        assert response.status == 200, response.text()
        actual = response.json()
        assert len(actual) == 1
        items = []
        for index, (state, _label, stage) in enumerate(CASES):
            item = deepcopy(actual[0])
            item.update({"intent_id": f"browser-cancel-{index}", "stage": stage,
                         "cancel_state": state, "journal_status": "SUBMISSION_UNKNOWN",
                         "account_stopped": True, "booked_to_portfolio": False,
                         "reservation_closed": False, "reserved_quote": "100.1",
                         "external_status": "CANCELED" if index == 3 else "NEW"})
            if index == 0:
                item["origin"] = {"status": "ARCHIVED_PAPER", "strategy_version_id": "version-test",
                                  "dataset_hash": "a" * 64, "timeframe": "1h",
                                  "signal_at": "2026-10-15T12:00:00+00:00"}
            elif index == 1:
                item["origin"] = {"status": "INVALID_SOURCE"}
            if index == 3:
                item["reconciliation"] = {"status": "FINAL_WITHOUT_EXECUTION", "deltas": {},
                                           "fee_by_asset": {}, "fill_count": 0,
                                           "quote_fee_model_compatible": False}
            else:
                item["reconciliation"] = {"status": "ORDER_NOT_FINAL", "deltas": None,
                                           "fee_by_asset": None, "fill_count": 0,
                                           "quote_fee_model_compatible": False}
            items.append(item)
        route.fulfill(status=200, body=json.dumps(items), content_type="application/json")

    page.route("**/real/execution/mexc/intents", mock_execution)
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-mexc-execution summary").wait_for()
    page.locator("#real-mexc-execution summary").click()
    cards = page.locator("#real-mexc-execution-list .real-exec-card")
    expect(cards).to_have_count(4)
    expect(page.locator(".real-mexc-head").first).to_contain_text("SIN ENVÍO A MEXC")
    for index, (_state, label, _stage) in enumerate(CASES):
        expect(cards.nth(index)).to_contain_text(label)
        expect(cards.nth(index)).to_contain_text("reserva local")
    expect(cards.nth(3)).to_contain_text("MEXC: CANCELED")
    expect(cards.nth(0).locator(".real-exec-origin")).to_contain_text("PAPER 1h")
    expect(cards.nth(1).locator(".real-exec-origin.invalid")).to_contain_text("Origen no verificable")
    assert cards.locator(".real-exec-book").count() == 0
    assert cards.locator(".real-exec-stop").count() == 0
    page.screenshot(path="/tmp/orbis-mexc-cancel-states.png", full_page=True)
    assert not errors, errors
    browser.close()
    print("MEXC cancelación: intento → acuse → aún abierta → final, UI sin liberar reserva")
