"""La UI no presenta un archivo MEXC inconsistente como listo para cerrar."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "browser booking vault password"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def inconsistent_archive(route):
        response = route.fetch()
        items = response.json()
        assert len(items) == 1
        items[0]["stage"] = "RECONCILIATION_INCONSISTENT"
        items[0]["booked_to_portfolio"] = True
        items[0]["reconciliation"]["status"] = "FILL_TOTAL_MISMATCH"
        route.fulfill(response=response, json=items)

    page.route("**/real/execution/mexc/intents", inconsistent_archive)
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear").click()
    page.locator("#real-mexc-execution summary").wait_for()
    page.locator("#real-mexc-execution summary").click()
    execution = page.locator("#real-mexc-execution-list")
    expect(execution).to_contain_text("Conciliación inconsistente · revisar")
    expect(execution).to_contain_text("No importes ni cierres desde esta vista")
    assert execution.locator(".real-exec-book").count() == 0
    assert execution.locator(".real-exec-close").count() == 0
    assert not errors, errors
    browser.close()
