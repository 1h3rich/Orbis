"""Hueco DCA en navegador: sólo investigación, nunca compra retrospectiva."""

from playwright.sync_api import expect, sync_playwright


START = 1_735_689_600
DAY = 86_400
URL = "http://127.0.0.1:8801"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(URL + "/ui/#/dca")
    page.wait_for_load_state("networkidle")
    page.locator("#dca-rule").select_option(index=1)
    page.locator("#dca-paper-dataset").select_option(index=1)
    page.locator("#dca-paper-start").click()
    page.locator("#dca-paper-report .statusbadge.on").wait_for()
    session_id = page.locator("#dca-paper-session").input_value()
    assert page.request.post(URL + "/__test/advance_paper_dca",
                             data={"now": START + 5 * DAY + 1}).ok
    page.locator("#dca-paper-report .statusbadge").filter(has_text="GAP").wait_for(timeout=20000)
    expect(page.locator("#dca-paper-poll")).to_be_disabled()
    page.locator("#dca-paper-gap").click()
    box = page.locator("#dca-paper-gap-report")
    expect(box).to_contain_text("TEORÍA · 2 días")
    expect(box).to_contain_text("guardado local")
    expect(box.locator("tbody tr")).to_have_count(2)
    before = page.request.get(URL + "/automation/dca/paper/sessions/" + session_id).json()
    assert before["status"] == "GAP" and before["events"] == [] and before["state"]["spent"] == "0"
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#dca-paper-session").select_option(session_id)
    expect(page.locator("#dca-paper-gap-report")).to_contain_text("guardado local")
    expect(page.locator("#dca-paper-gap-report tbody tr")).to_have_count(2)
    assert page.request.post(URL + "/__test/advance_paper_dca",
                             data={"now": START + 6 * DAY + 1}).ok
    page.locator("#dca-paper-gap").click()
    expect(page.locator("#dca-paper-gap-report")).to_contain_text("TEORÍA · 3 días")
    expect(page.locator("#dca-paper-gap-archive option")).to_have_count(2)
    first_report = page.locator("#dca-paper-gap-archive option").filter(has_text="2 d").get_attribute("value")
    page.locator("#dca-paper-gap-archive").select_option(first_report)
    expect(page.locator("#dca-paper-gap-report")).to_contain_text("TEORÍA · 2 días")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#dca-paper-session").select_option(session_id)
    expect(page.locator("#dca-paper-gap-archive option")).to_have_count(2)
    expect(page.locator("#dca-paper-gap-report")).to_contain_text("TEORÍA · 3 días")
    after = page.request.get(URL + "/automation/dca/paper/sessions/" + session_id).json()
    assert after == before
    assert not errors, errors
    print("Browser DCA GAP: informe teórico, recarga y saldo PAPER intacto OK")
    browser.close()
