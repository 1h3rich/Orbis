"""Download, review and reimport DCA through the normal browser controls."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.browser_network import route_once


BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUT = Path(os.environ["ORBIS_AUTOMATION_PATH"]).parent.parent
DCA = BASE + "/automation/dca"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/market/**", lambda route: route.fulfill(status=503, json={}))
    try:
        page.goto(BASE + "/ui/#/dca")
        page.wait_for_load_state("networkidle")
        plan = page.locator("#dca-rule")
        version = page.locator("#dca-version")
        file_input = page.locator("#dca-import-file")
        review = page.get_by_role("region", name="Revisar plan importado")
        expect(plan.locator("option")).to_have_count(2)
        plan.select_option(index=1)
        expect(version).not_to_have_value("")
        original_id, original_version = plan.input_value(), version.input_value()
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="Exportar versión").click()
        assert downloaded.value.suggested_filename == "orbis-dca-v1.json"
        archive = downloaded.value.path().read_bytes()
        bundle = json.loads(archive)
        assert bundle["schema"] == "orbis.automation.dca.v1"
        assert original_id.encode() not in archive and original_version.encode() not in archive

        def attach(raw):
            file_input.set_input_files({"name":"plan.json", "mimeType":"application/json", "buffer":raw})

        attach(archive)
        expect(review.get_by_text("Huella verificada", exact=False)).to_be_visible()
        expect(plan).to_have_value(original_id)
        assert len(page.request.get(DCA + "/rules").json()) == 1
        review.get_by_role("button", name="Cancelar").click()
        expect(review).to_be_hidden()
        assert len(page.request.get(DCA + "/rules").json()) == 1

        attach(archive)
        expect(review).to_be_visible()
        saved = {}

        def lose_reply(route):
            response = route.fetch()
            assert response.ok, response.text()
            saved["headers"] = route.request.headers
            saved["result"] = response.json()
            route.abort()

        route_once(page, "**/rules/import", lose_reply)
        copy = review.get_by_role("button", name="Guardar copia")
        copy.click()
        expect(page.locator("#dca-notice.error")).to_be_visible()
        expect(copy).to_be_enabled()
        with page.expect_response(lambda response: response.url == DCA + "/rules/import"
                                  and response.request.method == "POST") as retry:
            copy.click()
        assert retry.value.ok, retry.value.text()
        assert retry.value.json() == saved["result"]
        assert retry.value.request.headers["x-orbis-import-id"] == saved["headers"]["x-orbis-import-id"]
        copied = retry.value.json()
        assert copied["id"] != original_id and copied["version_id"] != original_version
        expect(plan).to_have_value(copied["id"])
        expect(version).to_have_value(copied["version_id"])
        assert len(page.request.get(DCA + "/rules").json()) == 2
        assert plan.locator("option").all_text_contents()[1] != plan.locator("option").all_text_contents()[2]
        assert page.request.get(DCA + "/paper/sessions").json() == []

        page.get_by_role("button", name="Comparar", exact=True).click()
        report = page.locator("#dca-report")
        report.get_by_role("button", name="Verificar", exact=True).click()
        expect(report.get_by_text("REPRODUCIBLE", exact=True)).to_be_visible()
        run_id = report.get_attribute("data-run-id")
        run = page.request.get(DCA + "/runs/" + run_id).json()
        assert run["version_id"] == copied["version_id"] and run["rule"] == bundle["plan"]["definition"]

        template = {"name":"DCA externo <img src=x>", "definition":bundle["plan"]["definition"]}
        attach(json.dumps(template).encode())
        expect(review.get_by_text("Plantilla JSON", exact=False)).to_be_visible()
        expect(review.locator("#dca-import-name")).to_have_text(template["name"])
        expect(review.locator("img")).to_have_count(0)
        review.get_by_role("button", name="Cancelar").click()
        page.reload()
        page.wait_for_load_state("networkidle")
        expect(plan.locator("option")).to_have_count(3)
        plan.select_option(copied["id"])
        expect(version).to_have_value(copied["version_id"])
        assert not errors, errors
        (OUT / "dca-import-result.json").write_text(json.dumps({
            "verified_checksum": True, "cancel_did_not_write": True, "replayed_after_lost_response": True,
            "copies": 1, "recovered_after_reload": True, "reproducible_backtest": True, "paper_sessions": 0,
        }, indent=2))
    finally:
        browser.close()
