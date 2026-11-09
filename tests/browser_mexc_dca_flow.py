"""Navegador → API → SQLCipher → trabajador → fills MEXC ficticios → UI."""
from tests.browser_network import route_once

import os
import json
from pathlib import Path

from playwright.sync_api import expect,sync_playwright
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
average = os.environ.get("ORBIS_TEST_MEXC_DCA_AVERAGE") == "1"
volume = os.environ.get("ORBIS_TEST_MEXC_DCA_VOLUME") == "1"
paper_handoff = average or volume
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors,dialogs = [],[]
    page.on("pageerror",lambda error:errors.append(str(error)))
    def accept(dialog):
        dialogs.append(dialog.message)
        dialog.accept()
    page.on("dialog",accept)
    page.goto(BASE+("/ui/#/dca" if paper_handoff else "/ui/#/real"))
    page.wait_for_load_state("networkidle")
    source_id = None
    if paper_handoff:
        sessions = page.locator("#dca-paper-session")
        expect(sessions.locator("option")).to_have_count(2)
        sessions.select_option(index=1)
        source_id = sessions.input_value()
        page.get_by_role("button", name="Preparar MEXC REAL", exact=True).click()
        expect(page).to_have_url(BASE+"/ui/#/real")
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
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("autorización de operativa separada")
    policies = page.locator("#real-mexc-policy-drafts")
    policies.locator("summary").click()
    expect(policies.locator('[name="strategy_version_id"] option')).to_contain_text(["Selecciona versión LONG / DCA","Bot nativo de prueba","DCA · DCA probado"])
    panel = page.locator("#real-mexc-bots")
    if not panel.locator(":scope > details").evaluate("node => node.open"):
        panel.locator("summary").click()
    expect(panel.locator('[name="paper"] option')).to_have_count(3)
    dca_value = panel.locator('[name="paper"] option').filter(has_text="DCA · DCA probado").get_attribute("value")
    if paper_handoff:
        assert source_id == dca_value
        expect(panel.locator('[name="paper"]')).to_have_value(source_id)
    else:
        panel.locator('[name="paper"]').select_option(dca_value)
    expect(panel.locator("[data-bots-dca-limits]")).to_be_visible()
    panel.locator('[name="budget"]').fill("30" if average else "20")
    assert not panel.locator("[data-bots-form]").evaluate("form => form.checkValidity()")
    panel.locator('[name="dca_total"]').fill("100")
    panel.locator('[name="dca_monthly"]').fill("40")
    assert panel.locator("[data-bots-form]").evaluate("form => form.checkValidity()")
    saved = {}
    activation_requests = []
    page.on("request", lambda req: activation_requests.append(req.post_data_json)
            if req.method=="POST" and req.url.endswith("/mexc-bots") else None)
    def lose_activation_reply(route):
        response = route.fetch()
        assert response.status == 200,response.text()
        saved.update(url=route.request.url,headers=route.request.headers,body=route.request.post_data_json)
        route.abort()
    route_once(page,"**/mexc-bots",lose_activation_reply)
    panel.get_by_role("button",name="Activar bot",exact=True).click()
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo",timeout=15000)
    assert "DCA sólo compras" in dialogs[-1] and "No vende ni coloca stops" in dialogs[-1]
    assert saved["body"]["source_kind"] == "DCA"
    assert saved["body"]["research_review_digest"]
    expect(panel.locator("[data-bots-state]")).to_have_text("Autorización registrada · respuesta recuperada.")
    expect(panel.get_by_role("button",name="Activar bot",exact=True)).to_be_disabled()
    assert activation_requests == [saved["body"]]
    replay = page.request.post(saved["url"], headers=saved["headers"], data=saved["body"])
    assert replay.status == 200,replay.text()
    assert replay.json()["id"] == saved["body"]["request_id"]
    assert replay.json()["research_binding"]["sha256"] == saved["body"]["research_review_digest"]
    status = page.request.get(BASE+"/__test/posts").json()
    assert status["count"] == 0 and len(status["workers"]) == 1
    assert page.request.post(BASE+"/__test/advance/2").json()["changed"]
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 2",timeout=15000)
    expect(panel.locator("[data-bots-list]")).to_contain_text("Mercado REAL")
    assert page.request.get(BASE+"/__test/posts").json()["count"] == 2
    expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(2)
    expect(page.locator("#real-history").get_by_text("SELL",exact=True)).to_have_count(0)
    panel.get_by_role("button",name="Diario",exact=True).click()
    expect(panel.locator("[data-bot-journal]")).to_contain_text("BUY · DCA_BASE")
    expect(panel.locator("[data-bot-journal]")).to_contain_text("BUY · DCA_DIP")
    if average:
        expect(panel.locator("[data-bot-journal]").get_by_text("EMA 2", exact=False)).to_have_count(2)
        expect(panel.locator("[data-bot-journal]")).to_contain_text("×0.25")
    if volume:
        expect(panel.locator("[data-bot-journal]").get_by_text("Volumen relativo", exact=False)).to_have_count(2)
        assert replay.json()["research_binding"]["paper"]["simulation_engine"] == "dca-1.9"
    panel.get_by_role("button",name="Gráfico",exact=True).click()
    graph = panel.locator("[data-bots-chart]")
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-fill-count","2")
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-buy-count","2")
    expect(graph.locator("[data-real-chart-inspect]")).to_contain_text("DCA_")
    with page.expect_download() as downloaded:
        graph.get_by_role("button",name="Exportar JSONL",exact=True).click()
    rows = [json.loads(line) for line in Path(downloaded.value.path()).read_bytes().splitlines()]
    assert len(rows) == 2 and all(row["fill"]["side"] == "BUY" and
        row["provenance"]["strategy_kind"] == "DCA" and row["provenance"]["authorization_index"] == 1 for row in rows)
    assert all(row["provenance"]["reserve"]["artifact_id"] == saved["body"]["research_validation_id"] for row in rows)
    panel.screenshot(path="/tmp/orbis-dca-real-desktop.png")
    panel.get_by_role("button",name="Detener bot",exact=True).click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    # Sólo el rearme vuelve a revisar un origen PAPER actual; no produjo las compras REAL.
    assert page.request.post(BASE+"/__test/observe/2").json()["poll_result"] == "OBSERVED"
    panel.get_by_role("button",name="Reanudar",exact=True).click()
    assert panel.locator('[name="dca_total"]').input_value() == "100"
    assert panel.locator('[name="dca_monthly"]').input_value() == "40"
    assert panel.locator('[name="dca_total"]').evaluate("input => input.readOnly")
    assert panel.locator('[name="dca_monthly"]').evaluate("input => input.readOnly")
    with page.expect_response("**/mexc-bots/*/rearm") as rearm:
        panel.get_by_role("button",name="Revisar y reanudar",exact=True).click()
    assert rearm.value.status == 200,rearm.value.text()
    assert rearm.value.json()["position"]["fill_count"] == 2
    assert rearm.value.json()["dca_limits"] == {"budget_total_quote":"100","monthly_limit_quote":"40"}
    assert page.request.post(BASE+"/__test/advance/3").json()["changed"]
    panel.get_by_role("button",name="Actualizar",exact=True).click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 2")
    if average:
        expect(panel.locator("[data-bots-list]")).to_contain_text("Importe ×0", timeout=15000)
    if volume:
        expect(panel.locator("[data-bots-list]")).to_contain_text("Volumen por debajo del mínimo", timeout=15000)
    assert page.request.get(BASE+"/__test/posts").json()["count"] == 2
    page.set_viewport_size({"width":390,"height":844})
    page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth+1")
    panel.screenshot(path="/tmp/orbis-dca-real-mobile.png")
    page.get_by_role("button",name="Bloquear",exact=True).click()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    panel.locator("summary").click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 2")
    panel.get_by_role("button",name="Gráfico",exact=True).click()
    expect(panel.locator("[data-real-chart-canvas]")).to_have_attribute("data-fill-count","2")
    storage = page.evaluate("JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])")
    assert "fixture-secret" not in storage and PASSWORD not in storage
    assert page.request.get(BASE+"/__test/posts").json()["count"] == 2
    assert not errors,errors
    if average:
        out = Path(os.environ["ORBIS_TEST_DCA_DIRECTORY"]).parent
        (out/"mexc-dca-average-result.json").write_text(json.dumps({
            "paper_handoff": saved["body"]["paper_session_id"] == source_id,
            "adjusted_journal": True, "fills": 2, "zero_amount_no_post": True,
            "rearm_verified": True, "cold_ui_verified": True}, indent=2))
    if volume:
        out = Path(os.environ["ORBIS_TEST_DCA_DIRECTORY"]).parent
        (out/"mexc-dca-volume-result.json").write_text(json.dumps({
            "paper_handoff":saved["body"]["paper_session_id"] == source_id, "volume_journal":True,
            "fills":2, "below_volume_no_post":True, "rearm_verified":True, "cold_ui_verified":True,
        }, indent=2))
    browser.close()
    print("DCA UI: prueba fijada → límites REAL → respuesta perdida sin duplicación → velas propias REAL sin avanzar PAPER → BASE/DIP → fills/gráfico → rearme sin recargar presupuesto → bloqueo/reapertura/móvil OK · exchange ficticio")
