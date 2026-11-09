"""Continuo UI → día 2 sin PAPER → BUY/SELL → parada, con transporte ficticio."""

import os
import time

from playwright.sync_api import expect,sync_playwright
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors,dialogs = [],[]
    page.on("pageerror",lambda error:errors.append(str(error)))
    def accept(dialog): dialogs.append(dialog.message); dialog.accept()
    page.on("dialog",accept)
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
    panel.locator('[name="permission"]').select_option("CONTINUOUS")
    expect(panel.locator('[name="continuous_until"]')).to_be_visible()
    value = page.evaluate("() => {const d=new Date(Date.now()+3*86400000);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}")
    panel.locator('[name="continuous_until"]').fill(value)
    with page.expect_response(lambda r:r.request.method == "POST" and r.url.endswith("/mexc-bots")) as created:
        panel.get_by_role("button",name="Activar bot",exact=True).click()
    assert created.value.status == 200,created.value.text()
    root = created.value.json()
    assert root["execution_mode"] == "CONTINUOUS" and "PERMISO CONTINUO hasta" in dialogs[-1]
    panel.screenshot(path="/tmp/orbis-continuity-controls.png")
    page.set_viewport_size({"width":390,"height":844})
    page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth+1")
    panel.screenshot(path="/tmp/orbis-continuity-controls-mobile.png")
    page.set_viewport_size({"width":1440,"height":1000})
    page.get_by_role("button",name="Proteger vista",exact=True).click()
    expect(page.locator("[data-operational-status]")).to_contain_text("Operativa activa")
    page.request.post(BASE+"/__test/close-paper")
    crossed = page.request.post(BASE+"/__test/drive-to/25",timeout=120000)
    assert crossed.status == 200,crossed.text()
    assert crossed.json()["orders"] == 0 and crossed.json()["worker"]["policy_id"] != root["policy_id"]
    assert page.request.get(BASE+"/real/vault/status").json()["operating"]["until"] == root["execution_permission"]["expires_at"]
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    panel.locator("summary").click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Continuo · hasta")
    expect(panel.locator("[data-bots-list]")).to_contain_text("Renovaciones ·")
    expect(panel.locator('[data-bots-form-state]')).to_have_text("PAPER · no disponible")
    expect(panel.locator('[data-bots-state].error')).not_to_be_visible()
    expect(panel.locator('[data-bots-start]')).to_be_disabled()
    assert page.request.post(BASE+"/__test/drive-to/27",timeout=120000).status == 200
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 1",timeout=15000)
    page.request.post(BASE+"/__test/target")
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 2",timeout=15000)
    panel.screenshot(path="/tmp/orbis-continuity-panel.png")
    page.set_viewport_size({"width":390,"height":844})
    page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth+1")
    panel.screenshot(path="/tmp/orbis-continuity-mobile.png")
    page.set_viewport_size({"width":1440,"height":1000})
    result = page.request.get(BASE+"/__test/posts").json()
    assert result["count"] == 2 and len(result["workers"]) == 1
    page.get_by_role("button",name="Proteger vista",exact=True).click()
    if os.environ.get("ORBIS_TEST_CONTINUOUS_EXPIRY") == "1":
        expired = page.request.post(BASE+"/__test/expire-continuity")
        assert expired.status == 200,expired.text()
        expect(page.locator("[data-operational-status]")).to_have_count(0,timeout=15000)
        page.locator("#real-passphrase").fill(PASSWORD)
        page.get_by_role("button",name="Desbloquear",exact=True).click()
        if not panel.locator("details").evaluate("element => element.open"): panel.locator("summary").click()
        expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
        assert page.request.get(BASE+"/__test/posts").json()["count"] == 2
        assert page.request.get(BASE+"/__test/posts").json()["workers"][0]["stop_requested"]
        page.get_by_role("button",name="Bloquear",exact=True).click()
    else:
        page.get_by_role("button",name="Detener y bloquear",exact=True).click()
    expect(page.locator("[data-operational-status]")).to_have_count(0)
    assert not page.request.get(BASE+"/real/vault/status").json()["unlocked"]
    assert "fixture-secret" not in page.content() and not errors,errors
    storage = page.evaluate("() => JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])")
    assert PASSWORD not in storage and "fixture-secret" not in storage
    browser.close()
    print("Continuo UI: fecha explícita → vista protegida/PAPER cerrado → renovación día 2 → misma concesión/hebra → BUY/SELL → parada/caducidad y clave cerrada; móvil/secretos OK · exchange ficticio")
