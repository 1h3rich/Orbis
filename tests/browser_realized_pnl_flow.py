"""Un cierre REAL conserva el resultado realizado visible tras reiniciar sesión.

Requiere un servidor local aislado en el puerto 8798 con bóveda temporal
y ORBIS_PRICE_PROVIDER=manual para comprobar la valoración incompleta.
"""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8798/ui/#/real"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("isolated realized browser password")
    page.locator("#real-passphrase-confirm").fill("isolated realized browser password")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator("#real-new-account").wait_for()
    page.locator('#real-new-account [name="name"]').fill("Prueba de resultado")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-event-form").wait_for()

    form = page.locator("#real-event-form")
    form.locator("#real-kind").select_option("CONTRIBUTION")
    form.locator('[name="amount"]').fill("1000")
    form.get_by_role("button", name="Guardar movimiento").click()
    expect(page.locator("#real-history tbody tr")).to_have_count(1)

    form.locator("#real-kind").select_option("BUY")
    form.locator('[name="quantity"]').fill("1")
    form.locator('[name="unit_price"]').fill("100")
    form.get_by_role("button", name="Guardar movimiento").click()
    expect(page.locator("#real-history tbody tr")).to_have_count(2)
    expect(page.locator("#real-market-values")).to_contain_text(
        "Valoración EUR incompleta · sin precio actual"
    )

    form.locator("#real-kind").select_option("SELL")
    form.locator('[name="quantity"]').fill("1")
    form.locator('[name="unit_price"]').fill("120")
    form.get_by_role("button", name="Guardar movimiento").click()
    expect(page.locator("#real-history tbody tr")).to_have_count(3)
    expect(page.locator("#real-positions")).to_contain_text("Sin posiciones abiertas")
    expect(page.locator("#real-positions")).to_contain_text("Realizado: 20 EUR")
    expect(page.locator("#real-market-values")).to_contain_text("Patrimonio EUR: 1020")
    expect(page.locator("#real-market-values")).to_contain_text(
        "Resultado registrado EUR: 20 (realizado 20 · no realizado 0)"
    )

    page.get_by_role("button", name="Bloquear").click()
    page.locator("#real-passphrase").fill("isolated realized browser password")
    page.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#real-positions")).to_contain_text("Realizado: 20 EUR")
    expect(page.locator("#real-market-values")).to_contain_text("Resultado registrado EUR: 20")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    browser.close()
