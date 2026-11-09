"""Bloquear con archivo corrupto y respuesta perdida: datos ocultos, claves revocadas."""
from tests.browser_network import route_once

import os

from playwright.sync_api import expect,sync_playwright
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
OPERATIONAL = os.environ.get("ORBIS_TEST_OPERATIONAL_LOCK") == "1"
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
    panel = page.locator("#real-mexc-bots")
    panel.locator("summary").click()
    expect(panel.locator('[name="paper"] option')).to_have_count(2)
    panel.locator('[name="paper"]').select_option(index=1)
    panel.locator('[name="budget"]').fill("20")
    if OPERATIONAL: panel.locator('[name="permission"]').select_option("OPERATIONAL")
    panel.get_by_role("button",name="Activar bot",exact=True).click()
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    if OPERATIONAL:
        page.get_by_role("button",name="Proteger vista",exact=True).click()
        expect(page.locator("[data-operational-status]")).to_contain_text("Operativa activa")
    assert page.request.post(BASE+"/__test/corrupt-market").json()["changed"]
    with page.expect_response("**/real/operations/stop" if OPERATIONAL else "**/real/vault/lock") as locked:
        page.get_by_role("button",name="Detener y bloquear" if OPERATIONAL else "Bloquear",exact=True).click()
    assert locked.value.status == 200 and locked.value.json() == {"locked":True,"review_required":True},locked.value.text()
    expect(page.locator("#real-passphrase")).to_be_visible()
    expect(page.locator("#real-notice")).to_contain_text("el archivo del bot requiere revisión")
    assert not page.request.get(BASE+"/real/vault/status").json()["unlocked"]
    assert page.request.get(BASE+"/__test/posts").json()["count"] == 0
    page.screenshot(path="/tmp/orbis-bot-lock-invalid.png",full_page=True)
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    expect(page.get_by_role("button",name="Bloquear",exact=True)).to_be_visible()
    def lose_response(route):
        response = route.fetch()
        assert response.status == 200 and response.json()["locked"]
        route.abort()
    route_once(page,"**/real/vault/lock",lose_response)
    page.get_by_role("button",name="Bloquear",exact=True).click()
    expect(page.locator("#real-passphrase")).to_be_visible()
    expect(page.locator("#real-notice")).to_contain_text("no se pudo confirmar el bloqueo")
    expect(page.locator("#real-history")).to_have_count(0)
    assert not page.request.get(BASE+"/real/vault/status").json()["unlocked"]
    state = page.request.get(BASE+"/__test/posts").json()
    assert state["count"] == 0 and all(not worker["running"] for worker in state["workers"])
    assert "fixture-secret" not in page.content()
    assert not errors,errors
    browser.close()
    print("Bloqueo UI: archivo inválido → permisos/clave revocados y aviso; respuesta de bloqueo perdida → vista protegida sin afirmar confirmación; cero órdenes")
