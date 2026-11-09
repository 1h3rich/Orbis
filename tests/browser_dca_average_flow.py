"""Save and verify variable DCA amounts based on a causal EMA."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUT = Path(os.environ["ORBIS_AUTOMATION_PATH"]).parent.parent
START = 1_735_689_600
DAY = 86_400


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900}, timezone_id="UTC")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/market/**", lambda route: route.fulfill(status=503, json={}))
    try:
        page.goto(BASE + "/ui/#/dca")
        page.wait_for_load_state("networkidle")
        form = page.locator("#dca-form")
        mode = form.get_by_label("Condición de compra")
        matched = form.get_by_label("Si cumple · ×", exact=True)
        other = form.get_by_label("Si no cumple · ×", exact=True)
        expect(matched).to_be_hidden()
        mode.select_option("ema-above")
        expect(matched).to_be_visible()
        form.get_by_label("Periodo · velas").fill("3")
        form.get_by_label("Frecuencia").select_option("daily")
        matched.fill("1.5")
        other.fill("0.5")
        form.get_by_label("Nombre").fill("Aportación variable EMA")
        with page.expect_response(lambda response: response.url.endswith("/automation/dca/rules")
                                  and response.request.method == "POST") as saved:
            form.get_by_role("button", name="Guardar plan").click()
        assert saved.value.ok, saved.value.text()
        version = saved.value.json()
        expected_rule = {"kind": "ema", "period": 3, "condition": "above",
                         "matched_factor": "1.5", "unmatched_factor": "0.5"}
        assert version["definition"]["ma_adjustment"] == expected_rule
        expect(page.locator("#dca-version")).to_have_value(version["version_id"])

        page.locator("#dca-run").click()
        expect(page.locator("[data-dca-sma-summary]")).to_have_text(
            "Cierre sobre EMA 3 · ×1.5 / ×0.5 · 2 señales descartadas")
        report = page.locator("#dca-report")
        report.get_by_role("button", name="Verificar").click()
        expect(report.get_by_text("REPRODUCIBLE", exact=True)).to_be_visible()
        page.locator("#dca-paper-dataset").select_option(index=1)
        expect(page.locator("#dca-paper-run")).not_to_have_value("")
        with page.expect_response(lambda response: response.url.endswith("/paper/sessions")
                                  and response.request.method == "POST") as started:
            page.get_by_role("button", name="Activar ficticio").click()
        assert started.value.ok, started.value.text()
        session = started.value.json()
        expect(page.locator("#dca-paper-session")).to_have_value(session["id"])
        paper = page.locator("#dca-paper-report")
        expect(paper.get_by_text("HISTÓRICO · VERIFICADO")).to_be_visible()
        expect(paper.locator("[data-dca-mexc-handoff]")).to_be_visible()

        def advance(day):
            response = page.request.post(BASE + "/__test/advance_paper_dca", data={"now": START + day * DAY + 1})
            assert response.ok, response.text()

        advance(5)
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(2, timeout=20_000)
        expect(paper.get_by_text("EMA 3 = ≈ 106,25 · ×0.5", exact=False)).to_have_count(2)
        advance(6)
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(3, timeout=20_000)
        expect(paper.get_by_text("EMA 3 = ≈ 123,125 · ×1.5", exact=False)).to_be_visible()

        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator("#dca-rule").select_option(version["id"])
        expect(mode).to_have_value("ema-above")
        expect(matched).to_have_value("1.5")
        expect(other).to_have_value("0.5")
        page.locator("#dca-paper-session").select_option(session["id"])
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(3)
        assert not errors, errors
        result = page.request.get(BASE + "/automation/dca/paper/sessions/" + session["id"]).json()
        buys = [event for event in result["events"] if event["kind"] == "BUY"]
        assert [event["amount_factor"] for event in buys] == ["0.5", "0.5", "1.5"]
        assert [event["spent"] for event in buys] == ["50.0", "25.0", "150.0"]
        (OUT / "average-result.json").write_text(json.dumps({
            "engine": result["engine_version"], "rule": result["ma_adjustment"], "spends": [event["spent"] for event in buys],
            "observations": result["tick_index"], "historic_verified": True, "restored_after_reload": True,
        }, indent=2))
    finally:
        browser.close()
