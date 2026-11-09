"""Save, backtest and observe a close-based SMA filter through the DCA UI."""

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
        condition = form.get_by_label("Condición de compra")
        period = form.get_by_label("Periodo · velas")
        expect(period).to_be_disabled()
        condition.select_option("below")
        expect(period).to_be_enabled()
        period.fill("2")
        form.get_by_label("Nombre").fill("DCA con SMA")
        with page.expect_response(BASE + "/automation/dca/rules") as saved:
            form.get_by_role("button", name="Guardar plan").click()
        assert saved.value.ok, saved.value.text()
        version = saved.value.json()
        assert version["definition"]["sma_gate"] == {"condition": "below", "period": 2}
        expect(page.locator("#dca-version")).to_have_value(version["version_id"])

        page.locator("#dca-run").click()
        expect(page.locator("[data-dca-sma-summary]")).to_have_text("Cierre bajo SMA 2 · 1 señal descartada")
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
            response = page.request.post(BASE + "/__test/advance_paper_dca",
                                         data={"now": START + day * DAY + 1})
            assert response.ok, response.text()

        advance(4)
        expect(paper.get_by_text("BASE · Fuera del filtro SMA · SMA 2 = ≈ 95")).to_be_visible(timeout=20_000)
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(0)
        advance(5)
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(3, timeout=20_000)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator("#dca-rule").select_option(version["id"])
        expect(condition).to_have_value("below")
        expect(period).to_have_value("2")
        page.locator("#dca-paper-session").select_option(session["id"])
        expect(paper.get_by_text("BUY", exact=True)).to_have_count(3)
        assert not errors, errors
        result = page.request.get(BASE + "/automation/dca/paper/sessions/" + session["id"]).json()
        assert result["state"] is not None and result["source_run_id"] == session["source_run_id"]
        assert [row["sma_value"] for row in result["events"] if row["kind"] == "BUY"] == ["92.5"] * 3
        (OUT / "sma-result.json").write_text(json.dumps({
            "engine": result["engine_version"], "rule": result["sma_gate"],
            "observations": result["tick_index"], "buy_count": 3,
            "historic_verified": True, "restored_after_reload": True,
        }, indent=2))
        page.screenshot(path=str(OUT / "dca-sma.png"), full_page=True)
    finally:
        browser.close()
