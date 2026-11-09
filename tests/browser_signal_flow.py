"""Recorrido navegador de señales LAB→PAPER, con datos GRVT controlados."""

import re
import time

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8766"
START = 1_735_689_600
STEP = 3600

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    encrypted = page.request.post(BASE + "/paper/perp/create", data={
        "passphrase": "isolated browser grvt paper password"
    })
    assert encrypted.status == 200, encrypted.text()
    page.goto(BASE + "/ui/#/perpetuos")
    page.wait_for_load_state("networkidle")
    datasets = page.request.get(BASE + "/lab/datasets").json()
    strategies = page.request.get(BASE + "/lab/strategies").json()
    reference = page.request.post(BASE + "/lab/runs", data={
        "dataset_id": datasets[0]["id"], "version_id": strategies[0]["version_id"]})
    assert reference.ok, reference.text()
    page.locator("#perp-account-form button[type=submit]").click()
    expect(page.locator("#perp-summary")).to_contain_text("10000 USDT")
    expect(page.locator("#perp-signal-form [name=version_id]")).to_contain_text("Corto 3 velas")
    expect(page.locator("#perp-signal-form [name=dataset_id]")).to_contain_text("BTC/USDT:PERP")
    page.locator("#perp-signal-create").click()
    expect(page.locator("#perp-signal-status")).to_contain_text("Seguimiento iniciado")
    expect(page.locator("#perp-signal-worker")).to_contain_text("ACTIVO")
    expect(page.locator("#perp-risk-worker")).to_contain_text("EN ESPERA")
    expect(page.locator("#perp-funding-worker")).to_contain_text("ACTIVO")
    expect(page.locator("#perp-signal-sessions")).to_contain_text("ACTIVE")
    page.locator('[data-signal-action="poll"]').click()
    expect(page.locator("#perp-signal-status")).to_contain_text("WAIT")
    page.request.post(BASE + "/__test/advance_signal", data={"now": START + 6 * STEP + 1})
    session_id = page.locator('[data-signal-action="inspect"]').get_attribute("data-session")
    page.wait_for_function("async id => { const r = await fetch('/paper/perp/signals/' + id); return (await r.json()).observations.length === 1; }", arg=session_id, timeout=10000)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator('[data-signal-action="inspect"]').click()
    expect(page.locator("#perp-signal-detail")).to_contain_text("SEÑAL")
    expect(page.locator("#perp-signal-detail")).to_contain_text("AUTO")
    expect(page.locator("#perp-signal-comparison")).to_contain_text("LAB · HISTÓRICO TEÓRICO")
    expect(page.locator("#perp-signal-comparison")).to_contain_text("PAPER · OBSERVADO")
    expect(page.locator("#perp-signal-comparison")).to_contain_text("✓ Verificado")
    expect(page.locator("#perp-signal-comparison .paper-signal-replay")).to_contain_text("Señales verificadas · 1 vela")
    expect(page.locator("#perp-signal-comparison .paper-frequency")).to_contain_text("Señales / 100 velas")
    expect(page.locator("#perp-signal-comparison .paper-frequency")).to_contain_text("muestra PAPER escasa")
    expect(page.locator("#perp-signal-comparison .paper-frequency-scope")).to_have_count(2)
    expect(page.locator("#perp-signal-comparison .paper-frequency-scope").nth(0)).to_have_attribute(
        "data-help", re.compile(r"LAB · velas evaluadas del .* UTC al .* UTC"))
    expect(page.locator("#perp-signal-comparison .paper-frequency-scope").nth(1)).to_have_attribute(
        "data-help", re.compile(r"PAPER · velas evaluadas del .* UTC al .* UTC"))
    tampered = page.request.post(BASE + "/__test/tamper_lab_result", data={"run_id": reference.json()["id"]})
    assert tampered.ok, tampered.text()
    page.locator('[data-signal-action="inspect"]').click()
    expect(page.locator("#perp-signal-comparison")).to_contain_text("Histórico no verificable")
    assert "999999" not in page.locator("#perp-signal-comparison").inner_text()
    assert "SIN ÓRDENES REALES" in page.locator(".perp-signals").inner_text()
    assert "Sin posiciones" in page.locator("#perp-positions").inner_text() or "Todavía no hay posiciones" in page.locator("#perp-positions").inner_text()
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator('[data-signal-action="inspect"]').click()
    expect(page.locator("#perp-signal-detail")).to_contain_text("SEÑAL")
    fresh = page.request.post(BASE + "/__test/extend_signal_seed").json()
    page.reload()
    page.locator("#perp-signal-form [name=dataset_id]").select_option(fresh["id"])
    page.locator("#perp-signal-form [name=execution_mode]").select_option("AUTO_PAPER")
    expect(page.locator("#perp-auto-fields")).to_be_visible()
    expect(page.locator("#perp-auto-strategy")).to_contain_text("SHORT · stop 3% · objetivo 8%")
    page.locator("#perp-auto-fields [name=size]").fill("1")
    page.locator("#perp-auto-fields [name=leverage]").fill("2")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#perp-signal-create").click()
    expect(page.locator("#perp-signal-status")).to_contain_text("Auto PAPER activado")
    auto_row = page.locator("#perp-signal-sessions tr").filter(has_text="AUTO PAPER")
    expect(auto_row).to_have_count(1)
    auto_id = auto_row.locator('[data-signal-action="inspect"]').get_attribute("data-session")
    page.request.post(BASE + "/__test/advance_signal", data={"now": START + 7 * STEP + 1})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = page.request.get(BASE + "/paper/perp/signals/" + auto_id).json()
        if detail["actions"] and detail["actions"][0]["status"] == "EXECUTED":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("La señal PAPER automática no abrió una posición")
    page.reload()
    expect(page.locator("#perp-positions")).to_contain_text("AUTO")
    page.locator('[data-signal-action="inspect"][data-session="' + auto_id + '"]').click()
    expect(page.locator("#perp-signal-detail")).to_contain_text("EXECUTED")
    expect(page.locator("#perp-signal-comparison")).to_contain_text("1 aperturas")
    comparison_url = "**/paper/perp/signals/" + auto_id + "/comparison"
    current = page.request.get(BASE + "/paper/perp/signals/" + auto_id + "/comparison").json()
    current["paper"].update({"closed_positions": 1, "open_positions": 0,
                             "realized_pnl_usdt": "7.5",
                             "realized_pnl_funding_covered_usdt": "0",
                             "realized_pnl_provisional_usdt": "7.5",
                             "funding_covered_closed": 0,
                             "funding_incomplete_closed": 1})
    page.route(comparison_url, lambda route: route.fulfill(status=200, json=current))
    page.locator('[data-signal-action="inspect"][data-session="' + auto_id + '"]').click()
    expect(page.locator("#perp-signal-comparison")).to_contain_text("P/L cubierto 0 USDT · provisional 7.5 USDT")
    expect(page.locator("#perp-signal-comparison")).to_contain_text("el P/L provisional puede cambiar")
    page.unroute(comparison_url)
    risk = page.request.post(BASE + "/__test/risk_quote", data={
        "session_id": auto_id, "now": START + 7 * STEP + 2,
        "mark": "200", "bid": "199", "ask": "201",
    })
    assert risk.status == 200 and risk.json()["outcome"] == "MARGIN_RISK", risk.text()
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#perp-positions")).to_contain_text("Riesgo de margen · revisar")
    expect(page.locator("#perp-events")).to_contain_text("RIESGO MARGEN")
    expect(page.locator("#perp-signal-sessions")).to_contain_text("no se simula liquidación")
    assert "Cerrar" in page.locator("#perp-positions").inner_text()

    def archived_gap(route):
        response = route.fetch()
        events = response.json()
        events.append({"id": "browser-gap", "account_id": events[-1]["account_id"],
                       "position_id": events[-1]["position_id"], "kind": "OBSERVE",
                       "request_id": None, "observed_at_ns": (START + 7 * STEP + 23) * 1_000_000_000,
                       "recorded_at": "2026-10-24T12:00:00+00:00",
                       "details": {"risk_type": "STREAM_COVERAGE_GAP",
                                   "last_accepted_quote_ns": (START + 7 * STEP + 2) * 1_000_000_000,
                                   "detected_at_ns": (START + 7 * STEP + 23) * 1_000_000_000,
                                   "incoming_quote_ns": None, "gap_ns": 21_000_000_000,
                                   "liquidation_status": "NOT_DETERMINED"}})
        route.fulfill(response=response, json=events)

    def gap_valuation(route):
        response = route.fetch()
        snapshots = response.json()
        for position_id in snapshots:
            snapshots[position_id] = {"status": "COVERAGE_GAP",
                                      "observed_at_ns": (START + 7 * STEP + 23) * 1_000_000_000,
                                      "remaining_ms": 0, "details": None,
                                      "last_risk_type": "STREAM_COVERAGE_GAP"}
        route.fulfill(response=response, json=snapshots)

    page.route("**/paper/perp/accounts/*/events", archived_gap)
    page.route("**/paper/perp/accounts/*/valuations", gap_valuation)
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#perp-events")).to_contain_text("SIN TICKER")
    expect(page.locator("#perp-events")).to_contain_text("≥21 s sin cobertura")
    expect(page.locator("#perp-positions")).to_contain_text("Sin ticker · cierre manual")
    expect(page.locator("#perp-positions")).to_contain_text("Sin ticker · posición abierta")
    assert not errors, errors
    print("LAB→PAPER UI: señales, posición ficticia y alarma de margen sin fill inventado OK")
    browser.close()
