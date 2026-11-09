"""Navegador → firma/orden → auditorías → conciliación → cierre, sin mainnet."""
from tests.browser_network import route_once

import json
from decimal import Decimal

from playwright.sync_api import expect,sync_playwright
from tests.grvt_testnet_ui_fixture import KEY,SIGNER,BUILDER,PASSPHRASE


BASE = "http://127.0.0.1:8799"
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors,submitted = [],[]
    page.on("pageerror",lambda error:errors.append(str(error)))
    page.on("dialog",lambda dialog:dialog.accept())
    page.on("request",lambda req:submitted.append(req) if req.method == "POST" and req.url.endswith("/signals/sign-and-submit") else None)
    page.goto(BASE+"/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSPHRASE)
    page.locator("#real-passphrase-confirm").fill(PASSPHRASE)
    page.get_by_role("button",name="Crear bóveda",exact=True).click()
    expect(page.locator("#real-grvt-testnet-send")).to_be_visible()
    opening = page.request.post(BASE+"/__test/seed").json()
    for closed in (False,True):
        source = page.request.post(BASE+"/__test/close").json() if closed else opening
        panel = page.locator("#real-grvt-testnet-send")
        if not panel.evaluate("node=>node.open"): panel.locator(":scope > summary").click()
        panel.locator('[name="sub_account_id"]').fill(source["sub_account_id"])
        panel.locator('[name="size"]').fill("0.5")
        panel.locator('[name="limit_price"]').fill(source["price"])
        credentials = panel.locator(".grvt-testnet-credentials")
        if not credentials.evaluate("node=>node.open"): credentials.locator("summary").click()
        panel.locator('[name="api_key"]').fill("fake-testnet-key-"+source["sub_account_id"])
        panel.locator('[name="dedicated_private_key"]').fill(KEY)
        panel.locator('[name="expected_signer"]').fill(SIGNER)
        panel.locator('[name="builder"]').fill(BUILDER)
        panel.get_by_role("button",name="Consultar señales",exact=True).click()
        expect(panel.locator('option[value="'+source["client_order_id"]+'"]').first).to_be_attached()
        panel.locator('[name="source"]').select_option(source["client_order_id"])
        with page.expect_response(lambda response:response.url.endswith("/signals/sign-and-submit")) as sent:
            panel.locator('[data-testnet-send]').click()
        assert sent.value.status == 200,sent.value.text()
        expect(panel.locator('[name="dedicated_private_key"]')).to_have_value("")
        result = page.locator("#real-grvt-testnet-recovery-result")
        row = result.locator("tbody tr").filter(has_text=source["client_order_id"])
        expect(row).to_be_visible()
        actions = page.locator("#real-grvt-testnet-actions")
        if not actions.evaluate("node=>node.open"): actions.locator(":scope > summary").click()
        for count in range(2):
            if count: page.wait_for_timeout(5100)  # Intervalo mínimo exigido por el protocolo.
            page.locator("#real-grvt-testnet-key").fill("fake-testnet-key-"+source["sub_account_id"])
            with page.expect_response(lambda response:response.url.endswith("/orders/audit")) as audit:
                row.get_by_role("button",name="Auditar",exact=True).click()
            assert audit.value.status == 200,audit.value.text()
            expect(page.locator("#real-grvt-testnet-key")).to_have_value("")
            if count == 0:
                expect(row.locator("[data-grvt-testnet-reconcile]")).to_have_count(0)
        reconcile = row.get_by_role("button",name="Conciliar · TESTNET",exact=True)
        expect(reconcile).to_be_enabled()
        if not closed:
            def lose_reply(route):
                response = route.fetch()
                assert response.status == 200,response.text()
                route.abort("failed")
            route_once(page,"**/real/exchanges/grvt/testnet/orders/reconcile",lose_reply)
        reconcile.click()
        expect(row).to_contain_text("Conciliada · TESTNET aislado")
        expect(row.get_by_role("button",name="Auditar",exact=True)).to_have_count(0)
        expect(row.locator("[data-grvt-testnet-reconcile]")).to_have_count(0)
        if closed and Decimal(opening["funding"]):
            costs = row.locator(".real-testnet-funding")
            expect(costs).to_be_visible()
            costs.locator("summary").click()
            expect(costs).to_contain_text("Neto "+opening["funding"]+" USDT")
            expect(costs).to_contain_text("Neto de orden "+str(Decimal("0.99")-Decimal(opening["funding"]))+" USDT")
    state = page.request.get(BASE+"/__test/state").json()
    assert len(submitted) == len(state["orders"]) == 2
    assert [order["side"] for order in state["orders"]] == ["BUY","SELL"]
    assert state["orders"][1]["reduce_only"] is True
    assert Decimal(state["balance"]) == Decimal("10000.98")-Decimal(opening["funding"]) and Decimal(state["position"]) == 0
    assert all("testnet.grvt.io" in url for url in state["calls"])
    storage = page.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
    assert KEY not in storage and "fake-testnet-key-" not in storage
    page.evaluate("window.scrollTo(0,0)")
    page.screenshot(path="/tmp/orbis-grvt-testnet-loop.png",full_page=True)
    page.set_viewport_size({"width":390,"height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
    page.screenshot(path="/tmp/orbis-grvt-testnet-loop-mobile.png",full_page=True)
    page.locator("#real-lock").click()
    expect(page.locator("#real-passphrase")).to_be_visible()
    # A delayed suggestion must not overwrite a field the user is editing.
    import time
    delayed=[]
    def hold_subaccounts(route):
        delayed.append((route,route.fetch()))
    route_once(page,"**/exchanges/grvt/testnet/subaccounts",hold_subaccounts)
    page.locator("#real-passphrase").fill(PASSPHRASE)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    form = page.locator("#real-grvt-testnet-recovery-form")
    field=form.locator('[name="sub_account_id"]')
    field.focus();field.fill("")
    deadline=time.monotonic()+5
    while not delayed:
        assert time.monotonic()<deadline
        page.wait_for_timeout(50)
    delayed[0][0].fulfill(response=delayed[0][1])
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    expect(field).to_have_value("")
    field.press_sequentially(source["sub_account_id"])
    expect(field).to_have_value(source["sub_account_id"])
    form.get_by_role("button",name="Consultar registro",exact=True).click()
    expect(page.locator("#real-grvt-testnet-recovery-result")).to_contain_text("Conciliada · TESTNET aislado")
    assert page.locator("#real-grvt-testnet-recovery-result tbody tr").count() == 2
    if Decimal(opening["funding"]): expect(page.locator(".real-testnet-funding")).to_have_count(1)
    assert len(page.request.get(BASE+"/__test/state").json()["orders"]) == 2
    assert not errors,errors
    browser.close()
    print("GRVT TESTNET: entrada, dos auditorías, conciliación con respuesta perdida, cierre y reapertura correctos")
