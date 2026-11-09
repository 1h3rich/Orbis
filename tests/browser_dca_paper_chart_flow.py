"""Las compras PAPER DCA observadas se anclan al tiempo/precio real del libro."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    for width in (1440, 390):
        page = browser.new_page(viewport={"width": width, "height": 850})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE + "/ui/#/dca")
        page.wait_for_load_state("networkidle")
        select = page.locator("#dca-paper-session")
        select.select_option(index=1)
        chart = page.locator("#dca-paper-chart")
        expect(chart).to_have_attribute("data-execution-count", "3")
        expect(chart).to_have_attribute("data-observed-candles", "1")
        expect(chart.locator("canvas").first).to_be_visible()
        expect(page.locator("#dca-paper-chart-detail")).to_contain_text("última ejecución sin OHLC")
        expect(page.locator("#dca-paper-chart-range")).to_have_attribute("data-view", "1")
        session_id = select.input_value()
        evidence = page.request.get(BASE + "/automation/dca/paper/sessions/" + session_id + "/chart").json()
        assert evidence["buys"][0]["execution_t"] > evidence["candles"][-1]["t"]
        assert evidence["buys"][0]["count"] == 3
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator("#dca-paper-session").select_option(session_id)
        expect(page.locator("#dca-paper-chart")).to_have_attribute("data-execution-count", "3")
        assert not errors, errors
        page.close()
    browser.close()

print("DCA PAPER: velas observadas, flechas en ejecución, recarga y móvil verificados")
