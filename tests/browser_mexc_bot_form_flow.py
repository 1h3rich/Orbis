"""Formulario REAL: rearme confirmado, mensajes acotados y controles verificables."""

import os
from playwright.sync_api import expect,sync_playwright
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors = []
    page.on("pageerror",lambda error:errors.append(str(error)))
    page.on("dialog",lambda dialog:dialog.accept())
    page.goto(BASE+"/ui/#/real"); page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button",name="Consultar MEXC",exact=True).click()
    page.locator("#real-mexc-trade-verify > summary").click()
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("fixture-trade")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button",name="Verificar Trade",exact=True).click()
    panel = page.locator("#real-mexc-bots"); panel.locator("summary").click()
    expect(panel.locator('[name="paper"] option')).to_have_count(2)
    panel.locator('[name="paper"]').select_option(index=1)
    panel.locator('[name="budget"]').fill("20")
    panel.locator('[name="permission"]').select_option("OPERATIONAL")
    with page.expect_response(lambda response:response.request.method == "POST" and response.url.endswith("/mexc-bots")) as activated:
        panel.get_by_role("button",name="Activar bot",exact=True).click()
    assert activated.value.status == 200,activated.value.text()
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    panel.get_by_role("button",name="Detener bot",exact=True).click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    panel.get_by_role("button",name="Reanudar",exact=True).click()
    with page.expect_response("**/mexc-bots/*/rearm") as rearmed:
        panel.get_by_role("button",name="Revisar y reanudar",exact=True).click()
    assert rearmed.value.status == 200,rearmed.value.text()
    expect(panel.locator("[data-bots-mode]")).to_have_text("Nuevo bot")
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    assert "mode is not a function" not in panel.inner_text()
    page.request.post(BASE+"/__test/close-paper")
    panel.get_by_role("button",name="Actualizar",exact=True).click()
    expect(panel.locator('[data-bots-form-state]')).to_have_text("PAPER · no disponible")
    expect(panel.locator('[data-bots-state].error')).not_to_be_visible()
    expect(panel.locator('[data-bots-start]')).to_be_disabled()
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    panel.get_by_role("button",name="Diario",exact=True).click()
    expect(panel.locator('[data-bot-journal]')).to_contain_text("Sin decisiones")
    panel.screenshot(path="/tmp/orbis-bot-form-scoped.png")
    page.set_viewport_size({"width":390,"height":844})
    page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth+1")
    panel.screenshot(path="/tmp/orbis-bot-form-scoped-mobile.png")
    page.set_viewport_size({"width":1440,"height":1000})
    result = page.request.get(BASE+"/__test/posts").json()
    assert result["count"] == 0 and len(result["workers"]) == 2 and result["workers"][0]["stop_requested"]
    panel.get_by_role("button",name="Detener bot",exact=True).click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    panel.get_by_role("button",name="Reanudar",exact=True).click()
    expect(panel.locator('[data-bots-mode]')).to_contain_text("Reanudar")
    expect(panel.locator('[data-bots-start]')).to_be_disabled()
    page.get_by_role("button",name="Bloquear",exact=True).click()
    assert not page.request.get(BASE+"/real/vault/status").json()["unlocked"]
    assert not errors,errors
    browser.close()
    print("Formulario REAL: rearme confirmado sin falso error → PAPER cerrado sólo bloquea nuevas autorizaciones → diario/estado/STOP accesibles; móvil y cero órdenes OK · exchange ficticio")
