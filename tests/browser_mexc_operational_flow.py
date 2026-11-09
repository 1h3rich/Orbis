"""Vista protegida → mercado propio/BUY → reautenticación/SELL → STOP sin contraseña."""

import os
import time

from playwright.sync_api import expect,sync_playwright
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
def posts(page,count):
    until = time.monotonic()+15
    result = None
    while time.monotonic()<until:
        result = page.request.get(BASE+"/__test/posts").json()
        if result["count"] == count: return result
        time.sleep(0.1)
    raise AssertionError(result)


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
    panel = page.locator("#real-mexc-bots")
    panel.locator("summary").click()
    expect(panel.locator('[name="paper"] option')).to_have_count(2)
    panel.locator('[name="paper"]').select_option(index=1)
    panel.locator('[name="budget"]').fill("20")
    assert panel.locator('[name="permission"]').input_value() == "SESSION"
    panel.locator('[name="permission"]').select_option("OPERATIONAL")
    with page.expect_response(lambda r:r.request.method == "POST" and r.url.endswith("/mexc-bots")) as created:
        panel.get_by_role("button",name="Activar bot",exact=True).click()
    assert created.value.status == 200,created.value.text()
    assert created.value.json()["execution_permission"]["protocol"] == "OPERATIONAL_V1"
    assert "La clave de la bóveda" in dialogs[-1] and "máximo 24 h" in dialogs[-1]
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    page.get_by_role("button",name="Proteger vista",exact=True).click()
    expect(page.locator("#real-passphrase")).to_be_visible()
    expect(page.locator("[data-operational-status]")).to_contain_text("Operativa activa")
    expect(page.locator(".pagina-cabecera .statusbadge")).to_have_text("VISTA PROTEGIDA")
    expect(page.locator("#real-history")).to_have_count(0)
    assert page.request.get(BASE+"/real/vault/status").json()["view_protected"]
    page.reload(); page.wait_for_load_state("networkidle")
    expect(page.locator("[data-operational-status]")).to_contain_text("Operativa activa")
    page.request.post(BASE+"/__test/close-paper")
    page.request.post(BASE+"/__test/market-only")
    assert len(posts(page,1)["workers"]) == 1
    page.screenshot(path="/tmp/orbis-operational-protected.png",full_page=True)
    page.set_viewport_size({"width":390,"height":844})
    page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth+1")
    page.screenshot(path="/tmp/orbis-operational-protected-mobile.png",full_page=True)
    page.set_viewport_size({"width":1440,"height":1000})
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    panel.locator("summary").click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 1")
    expect(panel.locator("[data-bots-list]")).to_contain_text("Operativo · hasta")
    page.request.post(BASE+"/__test/target")
    posts(page,2)
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 2",timeout=15000)
    assert len(page.request.get(BASE+"/__test/posts").json()["workers"]) == 1
    page.get_by_role("button",name="Proteger vista",exact=True).click()
    expect(page.locator("[data-operational-stop]")).to_be_visible()
    page.get_by_role("button",name="Detener y bloquear",exact=True).click()
    expect(page.locator("[data-operational-status]")).to_have_count(0)
    expect(page.locator("#real-notice")).to_contain_text("Operativa detenida")
    assert not page.request.get(BASE+"/real/vault/status").json()["unlocked"]
    result = page.request.get(BASE+"/__test/posts").json()
    assert result["count"] == 2 and all(not row["running"] for row in result["workers"])
    assert "fixture-secret" not in page.content()
    storage = page.evaluate("() => JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])")
    assert PASSWORD not in storage and "fixture-secret" not in storage
    assert not errors,errors
    browser.close()
    print("Operativo UI: opt-in → vista protegida/reload → PAPER cerrado → BUY propio → reautenticación sin nueva hebra → SELL → STOP local → clave cerrada, móvil/secretos OK · exchange ficticio")
