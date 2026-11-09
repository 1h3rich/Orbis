"""Chromium → API real → fuentes cifradas → firma real → proveedor ficticio."""
from tests.browser_network import route_once

import json

from playwright.sync_api import expect, sync_playwright

from tests.grvt_testnet_ui_fixture import KEY, SIGNER, BUILDER, PASSPHRASE


BASE = "http://127.0.0.1:8799"
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors, requests = [], []
    page.on("pageerror",lambda error:errors.append(str(error)))
    page.on("dialog",lambda dialog:dialog.accept())
    page.on("request",lambda request:requests.append(request) if request.method == "POST" and request.url.endswith("/signals/sign-and-submit") else None)
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSPHRASE)
    page.locator("#real-passphrase-confirm").fill(PASSPHRASE)
    page.get_by_role("button",name="Crear bóveda",exact=True).click()
    panel = page.locator("#real-grvt-testnet-send")
    expect(panel).to_be_visible()
    panel.locator(":scope > summary").click()
    expect(panel.locator("[data-testnet-state]")).to_contain_text("Sin acciones")

    def fill(source):
        panel.locator('[name="sub_account_id"]').fill(source["sub_account_id"])
        panel.locator('[name="size"]').fill("0.5")
        panel.locator('[name="limit_price"]').fill("100.01")
        details = panel.locator(".grvt-testnet-credentials")
        if not details.evaluate("node=>node.open"): details.locator("summary").click()
        panel.locator('[name="expected_signer"]').fill(SIGNER)
        panel.locator('[name="builder"]').fill(BUILDER)
        panel.locator('[name="api_key"]').fill("fake-testnet-key-" + source["sub_account_id"])
        panel.locator('[name="dedicated_private_key"]').fill(KEY)
        panel.get_by_role("button",name="Consultar señales",exact=True).click()
        option = panel.locator('option[value="' + source["client_order_id"] + '"]')
        expect(option).to_be_attached()
        # Nunca selecciona automáticamente una acción nueva.
        expect(panel.locator('[name="source"]')).to_have_value("")
        panel.locator('[name="source"]').select_option(source["client_order_id"])
        expect(panel.locator('[data-testnet-source]')).to_contain_text(source["direction"])
        expect(panel.locator('[data-testnet-send]')).to_be_enabled()

    for index,(direction,closing) in enumerate((("LONG",False),("SHORT",False),("LONG",True),("SHORT",True),("SHORT",False),("LONG",False))):
        source = page.request.post(BASE + "/__test/seed/" + direction + ("?closing=true" if closing else "")).json()
        if index:
            # El formulario conserva el intento anterior, nunca salta a otra señal.
            panel.locator('[name="source"]').select_option("")
        fill(source)
        if index == 4: page.request.post(BASE + "/__test/lost")
        if index == 5:
            page.request.post(BASE + "/__test/lost?lost=false")
            def lost_local_reply(route):
                response = route.fetch()
                assert response.status == 200
                route.abort("failed")
            route_once(page,"**/real/exchanges/grvt/testnet/signals/sign-and-submit",lost_local_reply)
            panel.locator('[data-testnet-send]').click()
        else:
            with page.expect_response(lambda response:response.url.endswith("/signals/sign-and-submit")) as sent:
                panel.locator('[data-testnet-send]').click()
            assert sent.value.status == (422 if index == 4 else 200), sent.value.text()
        expect(panel.locator('[name="api_key"]')).to_have_value("")
        expect(panel.locator('[name="dedicated_private_key"]')).to_have_value("")
        expect(panel.locator('[data-testnet-send]')).to_be_disabled()
        result = page.locator("#real-grvt-testnet-recovery-result")
        expect(result).to_contain_text(source["client_order_id"])
        expect(result).to_contain_text("PAPER · salida" if closing else "PAPER · entrada")
        expect(panel.locator('[data-testnet-state]')).to_contain_text("respuesta incierta" if index >= 4 else "Intento registrado")
        assert len(page.request.get(BASE + "/__test/state").json()["orders"]) == index + 1
        # Una segunda activación programática del submit tampoco manda nada.
        panel.locator("form").evaluate("node=>node.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
        assert len(requests) == index + 1

    payloads = [json.loads(request.post_data) for request in requests]
    assert [item["side"] for item in payloads] == ["BUY","SELL","SELL","BUY","SELL","BUY"]
    assert [item["reduce_only"] for item in payloads] == [False,False,True,True,False,False]
    assert all(item["confirmation"] == "FIRMAR_Y_ENVIAR_GRVT_TESTNET" for item in payloads)
    assert all("testnet.grvt.io" in url for url in page.request.get(BASE + "/__test/state").json()["calls"])
    storage = page.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
    assert KEY not in storage and "fake-testnet-key-" not in storage
    page.screenshot(path="/tmp/orbis-grvt-testnet-send.png",full_page=True)
    page.set_viewport_size({"width":390,"height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path="/tmp/orbis-grvt-testnet-send-mobile.png",full_page=True)
    panel.locator('[name="api_key"]').fill("fake-secret-to-clear")
    panel.locator('[name="dedicated_private_key"]').fill(KEY)
    panel.locator(":scope > summary").click()
    expect(panel.locator('[name="api_key"]')).to_have_value("")
    expect(panel.locator('[name="dedicated_private_key"]')).to_have_value("")
    panel.locator(":scope > summary").click()
    panel.locator('[name="api_key"]').fill("fake-secret-to-lock")
    page.locator("#real-lock").click()
    expect(page.locator("#real-passphrase")).to_be_visible()
    assert page.locator("#real-grvt-testnet-send").count() == 0
    page.locator("#real-passphrase").fill(PASSPHRASE)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    expect(page.locator("#real-grvt-testnet-recovery-result")).to_be_visible()
    form = page.locator("#real-grvt-testnet-recovery-form")
    form.locator('[name="sub_account_id"]').fill(source["sub_account_id"])
    form.get_by_role("button",name="Consultar registro",exact=True).click()
    expect(page.locator("#real-grvt-testnet-recovery-result")).to_contain_text(source["client_order_id"])
    # Misma función con una cuenta REAL existente y tras ocultar importes.
    page.locator('#real-new-account [name="name"]').fill("Cuenta local de prueba")
    page.get_by_role("button",name="Crear cuenta",exact=True).click()
    expect(page.locator("#real-account")).to_be_visible()
    panel = page.locator("#real-grvt-testnet-send")
    panel.locator(":scope > summary").click()
    panel.locator(".grvt-testnet-credentials summary").click()
    panel.locator('[name="api_key"]').fill("fake-secret-to-hide")
    page.locator("#real-hide").click()
    expect(page.get_by_role("button",name="Mostrar importes",exact=True)).to_be_visible()
    expect(panel.locator('[name="api_key"]')).to_have_value("")
    panel.locator(":scope > summary").click()
    expect(panel.locator('[data-testnet-send]')).to_be_disabled()
    page.set_viewport_size({"width":1440,"height":1000})
    page.evaluate("window.scrollTo(0,0)")
    page.screenshot(path="/tmp/orbis-grvt-testnet-existing-account.png",full_page=True)
    assert len(page.request.get(BASE + "/__test/state").json()["orders"]) == 6
    assert not errors,errors
    browser.close()
    print("GRVT testnet UI: LONG, SHORT, respuesta perdida, recuperación, claves efímeras y móvil correctos")
