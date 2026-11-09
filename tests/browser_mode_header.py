"""La cabecera distingue operativa ficticia de cartera REAL manual."""

from playwright.sync_api import expect, sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    for width in (1440, 390):
        page = browser.new_page(viewport={"width": width, "height": 850})
        page.add_init_script("localStorage.setItem('orbis.mode', 'LIVE')")
        page.goto("http://127.0.0.1:8000/ui/#/inicio")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("heading", name="Patrimonio registrado")).to_be_visible()
        assert page.get_by_text("Cartera REAL · separada de PAPER").count() == 0
        assert page.locator("#mode-badge").count() == 0
        assert page.locator("#mode-live, #mode-paper").count() == 0
        expect(page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Patrimonio")).to_be_visible()
        assert page.evaluate("window.OrbisUI.orbisMode()") == "PAPER"
        page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Patrimonio").click()
        expect(page.get_by_role("heading", name="Patrimonio", exact=True)).to_be_visible()
        expect(page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Patrimonio")).to_have_attribute("aria-current", "page")
        expect(page.get_by_role("navigation", name="Herramientas de simulación")).to_be_hidden()
        assert page.get_by_role("navigation", name="Vistas de patrimonio").count() == 0
        page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Simulación").click()
        expect(page.get_by_role("heading", name="Operativa simulada")).to_be_visible()
        expect(page.locator("#modeline")).to_contain_text("Saldo PAPER ficticio")
        expect(page.get_by_role("navigation", name="Herramientas de simulación")).to_be_visible()
        page.get_by_role("link", name="Ver saldo ficticio").click()
        expect(page.get_by_role("heading", name="Saldo ficticio")).to_be_visible()
        expect(page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Simulación")).to_have_attribute("aria-current", "page")
        page.get_by_role("link", name="Volver a operar").click()
        page.get_by_role("link", name="Perpetuos GRVT · PAPER manual").click()
        expect(page.get_by_role("heading", name="Perpetuos PAPER")).to_be_visible()
        page.get_by_role("navigation", name="Herramientas de simulación").get_by_role("link", name="Spot", exact=True).click()
        expect(page.get_by_role("heading", name="Spot PAPER")).to_be_visible()
        expect(page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Simulación")).to_have_attribute("aria-current", "page")
        page.get_by_role("navigation", name="Herramientas de simulación").get_by_role("link", name="DCA").click()
        expect(page.locator("#modeline")).to_contain_text("Simulación PAPER")
        page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Patrimonio").click()
        page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Laboratorio").click()
        expect(page.locator("#modeline")).to_contain_text("Laboratorio histórico")
        page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Patrimonio").click()
        expect(page.locator("#modeline")).to_contain_text("Patrimonio registrado")
        expect(page.get_by_role("navigation", name="Secciones").get_by_role("link", name="Patrimonio")).to_have_attribute("aria-current", "page")
        page.get_by_role("link", name="Estudio").click()
        expect(page.locator("#modeline")).to_contain_text("Mercado público")
        page.get_by_role("link", name="Configuración").click()
        assert page.locator("#mode-badge").count() == 0
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.goto("http://127.0.0.1:8000/ui/#/live")
        expect(page).to_have_url("http://127.0.0.1:8000/ui/#/inicio")
        assert page.get_by_text("Live (bloqueado)").count() == 0
        page.close()
    browser.close()
