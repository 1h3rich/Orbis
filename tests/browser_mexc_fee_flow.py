"""La UI muestra la comisión como hipótesis y no ofrece vincularla."""

from playwright.sync_api import expect, sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser fee hypothesis password")
    page.get_by_role("button", name="Desbloquear").click()
    option = page.locator("#real-account option").filter(has_text="MEXC comisión").first
    expect(option).to_have_count(1)
    page.locator("#real-account").select_option(value=option.get_attribute("value"))
    page.locator("#real-mexc-transfers-archive summary").click()
    page.locator("#real-mexc-transfers-list button").first.click()
    page.get_by_role("button", name="Comparar con cuenta seleccionada").click()
    result = page.locator("#real-mexc-transfer-candidates")
    expect(result).to_contain_text("Comisión: importe bruto/neto sin verificar")
    expect(result).to_contain_text("MEXC amount neto hipotético")
    expect(result).to_contain_text("Comisión · sin semántica verificada")
    expect(page.locator(".real-mexc-transfer-match")).to_have_count(0)
    assert not errors, errors
    browser.close()
    print("MEXC fee hypothesis browser flow passed: visible uncertainty, no link")
