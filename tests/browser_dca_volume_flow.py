"""Relative-volume plan, historical comparison and observed PAPER ledger."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUT = Path(os.environ["ORBIS_AUTOMATION_PATH"]).parent.parent
START, DAY = 1_735_689_600, 86_400


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440, "height":900}, timezone_id="UTC")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/market/**", lambda route: route.fulfill(status=503, json={}))
    try:
        page.goto(BASE + "/ui/#/dca")
        page.wait_for_load_state("networkidle")
        form = page.locator("#dca-form")
        condition = form.get_by_label("Condición de compra")
        ratio = form.get_by_label("Volumen mínimo · ×")
        expect(ratio).to_be_hidden()
        condition.select_option("volume")
        expect(ratio).to_be_enabled()
        ratio.fill("2")
        form.get_by_label("Periodo · velas").fill("2")
        form.get_by_label("Nombre").fill("DCA volumen relativo")
        with page.expect_response(BASE + "/automation/dca/rules") as saved:
            form.get_by_role("button", name="Guardar plan").click()
        assert saved.value.ok, saved.value.text()
        version = saved.value.json()
        assert version["definition"]["volume_gate"] == {"period":2, "min_ratio":"2"}
        expect(page.locator("#dca-version")).to_have_value(version["version_id"])

        page.locator("#dca-run").click()
        expect(page.locator("[data-dca-volume-summary]")).to_have_text("Volumen ≥ 2× · 2 velas · 2 señales descartadas")
        report = page.locator("#dca-report")
        report.get_by_role("button", name="Verificar").click()
        expect(report.get_by_text("REPRODUCIBLE", exact=True)).to_be_visible()
        run = page.locator("#dca-run-select").input_value()
        origin = page.request.get(BASE + "/automation/dca/runs/" + run).json()
        assert origin["engine_version"] == "dca-1.9"
        assert all(row["volume_ratio"] == "2" for row in origin["results"]["conditional"]["events"])
        assert origin["results"]["fixed"]["events"] and origin["results"]["lump"]["events"]
        page.locator("#dca-paper-dataset").select_option(index=1)
        expect(page.locator("#dca-paper-run")).to_have_value(run)
        with page.expect_response(lambda response: response.url.endswith("/paper/sessions")
                                  and response.request.method == "POST") as started:
            page.get_by_role("button", name="Activar ficticio").click()
        assert started.value.ok, started.value.text()
        session = started.value.json()
        expect(page.locator("#dca-paper-session")).to_have_value(session["id"])
        paper = page.locator("#dca-paper-report")
        expect(paper.get_by_text("HISTÓRICO · VERIFICADO")).to_be_visible()
        expect(paper.locator("[data-dca-mexc-handoff]")).to_be_visible()
        expect(paper.locator("[data-dca-dex-handoff]")).to_be_visible()

        def advance(day):
            response = page.request.post(BASE + "/__test/advance_paper_dca", data={"now":START + day * DAY + 1})
            assert response.ok, response.text()

        advance(5)
        expect(paper.get_by_text("BASE · Volumen por debajo del mínimo", exact=False)).to_be_visible(timeout=20_000)
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(0)
        advance(6)
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(3, timeout=20_000)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator("#dca-rule").select_option(version["id"])
        expect(condition).to_have_value("volume")
        expect(ratio).to_have_value("2")
        page.locator("#dca-paper-session").select_option(session["id"])
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(3)
        expect(paper.locator("[data-dca-mexc-handoff]")).to_be_visible()
        expect(paper.locator("[data-dca-dex-handoff]")).to_be_visible()
        result = page.request.get(BASE + "/automation/dca/paper/sessions/" + session["id"]).json()
        assert result["state"] is not None and result["source_run_id"] == run
        assert [row["volume_ratio"] for row in result["events"] if row["kind"] == "BUY"] == ["2"] * 3
        assert all(len(row["volume_window"]) == 3 for row in result["events"] if row["kind"] == "OBSERVATION")
        assert not errors, errors
        (OUT / "volume-result.json").write_text(json.dumps({
            "engine":result["engine_version"], "rule":result["volume_gate"], "historic_verified":True,
            "paper_buy_count":3, "restored_after_reload":True, "dex_handoff_available":True,
        }, indent=2))
        paper.screenshot(path=str(OUT / "dca-volume-paper.png"))
    finally:
        browser.close()
