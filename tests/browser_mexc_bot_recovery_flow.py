"""Preparación/POST interrumpidos → revisión explícita → mismo bot, cero reenvíos."""
from tests.browser_network import route_once

import os

from playwright.sync_api import expect,sync_playwright
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
MODE = os.environ["ORBIS_TEST_BOT_RECOVERY_MODE"]
assert MODE in ("before_submit","timeout")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors = []
    responses = []
    page.on("pageerror",lambda error:errors.append(str(error)))
    page.on("response",lambda response:responses.append((response.request.method,response.url.split("/real")[-1],response.status)) if "/real/" in response.url else None)
    page.on("dialog",lambda dialog:dialog.accept())
    page.goto(BASE+"/ui/#/real")
    page.wait_for_load_state("networkidle")
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
    panel.get_by_role("button",name="Activar bot",exact=True).click()
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    panel.get_by_role("button",name="Diario",exact=True).click()
    expect(panel.locator("[data-bot-journal]")).to_contain_text("Sin decisiones")
    page.request.post(BASE+"/__test/fault/"+MODE)
    assert page.request.post(BASE+"/__test/entry").json()["poll_result"] == "OBSERVED"
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado",timeout=15000)
    expected_posts = 0 if MODE == "before_submit" else 1
    assert page.request.get(BASE+"/__test/posts").json()["count"] == expected_posts
    action = "Parar y descartar" if MODE == "before_submit" else "Seguir orden"
    expected_status = "STOPPED" if MODE == "before_submit" else "RESERVATION_CLOSED"
    expect(panel.locator("[data-bot-journal]")).to_contain_text("RESERVED" if MODE == "before_submit" else "SUBMISSION_UNKNOWN")
    panel.screenshot(path="/tmp/orbis-bot-recovery-action-"+MODE+".png")
    saved = {}
    def lose_reply(route):
        response = route.fetch()
        assert response.status == 200,response.text()
        saved.update(url=route.request.url,headers=route.request.headers,body=route.request.post_data_json,result=response.json())
        if MODE == "timeout": assert saved["result"]["reservation_closed"],response.text()
        route.abort()
    suffix = "/release-unsent" if MODE == "before_submit" else "/synchronize"
    route_once(page,"**/execution/mexc/intents/*"+suffix,lose_reply)
    panel.get_by_role("button",name=action,exact=True).click()
    try:
        expect(panel.locator("[data-bot-journal]")).to_contain_text(expected_status,timeout=15000)
    except AssertionError:
        panel.screenshot(path="/tmp/orbis-bot-recovery-error-"+MODE+".png")
        raise AssertionError({"responses":responses[-18:],"state":panel.inner_text(),"recovery_attempted":bool(saved)}) from None
    expect(panel.get_by_role("button",name=action,exact=True)).to_have_count(0)
    assert page.request.get(BASE+"/__test/posts").json()["count"] == expected_posts
    replay = page.request.post(saved["url"],headers=saved["headers"],data=saved["body"])
    assert replay.status == 200,replay.text()
    if MODE == "before_submit":
        assert replay.json()["status"] == "ALREADY_RELEASED"
        expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(0)
    else:
        expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(1)
        expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 1")
    panel.screenshot(path="/tmp/orbis-bot-recovery-"+MODE+".png")
    panel.get_by_role("button",name="Reanudar",exact=True).click()
    with page.expect_response("**/mexc-bots/*/rearm") as rearm:
        panel.get_by_role("button",name="Revisar y reanudar",exact=True).click()
    assert rearm.value.status == 200,rearm.value.text()
    assert rearm.value.json()["position"]["fill_count"] == expected_posts
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    assert page.request.get(BASE+"/__test/posts").json()["count"] == expected_posts
    if MODE == "timeout":
        page.request.post(BASE+"/__test/target")
        expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 2",timeout=15000)
        expect(page.locator("#real-history").get_by_text("SELL",exact=True)).to_have_count(1)
        expected_posts = 2
    panel.get_by_role("button",name="Detener bot",exact=True).click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    assert page.request.get(BASE+"/__test/posts").json()["count"] == expected_posts
    page.set_viewport_size({"width":390,"height":844})
    panel.screenshot(path="/tmp/orbis-bot-recovery-mobile-"+MODE+".png")
    page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth+1")
    page.get_by_role("button",name="Bloquear",exact=True).click()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    panel.locator("summary").click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    panel.get_by_role("button",name="Diario",exact=True).click()
    expect(panel.locator("[data-bot-journal]")).to_contain_text(expected_status)
    assert page.request.get(BASE+"/__test/posts").json()["count"] == expected_posts
    # Una lectura fallida invalida la vista; no deja un diario antiguo verificado.
    def unreadable_diary(route): route.abort()
    page.route("**/mexc-bots/*/decisions",unreadable_diary)
    panel.get_by_role("button",name="Diario",exact=True).click()
    expect(panel.locator("[data-bot-journal]")).to_contain_text("Diario no verificable")
    expect(panel.locator("[data-bot-recover]")).to_have_count(0)
    page.unroute("**/mexc-bots/*/decisions",unreadable_diary)
    panel.get_by_role("button",name="Diario",exact=True).click()
    expect(panel.locator("[data-bot-journal]")).to_contain_text(expected_status)
    assert page.request.get(BASE+"/__test/posts").json()["count"] == expected_posts
    assert not errors,errors
    browser.close()
    print("Bot recovery UI:",MODE,"→ respuesta perdida → revisión/rearme sin reenvío, posición y asientos únicos, móvil/reapertura OK · exchange ficticio")
