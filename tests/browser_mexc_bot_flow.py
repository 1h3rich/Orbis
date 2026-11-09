"""UI: probar origen → autorizar bot → compra/venta autónoma → parada → bloqueo."""

from decimal import Decimal
import json
import os
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as BrowserTimeout, expect, sync_playwright
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors = []
    page.on("pageerror",lambda error:errors.append(str(error)))
    cash_rearm = os.environ.get("ORBIS_TEST_MEXC_CASH_REARM") == "1"
    manual_sale_rearm = os.environ.get("ORBIS_TEST_MEXC_SALE_REARM") == "1"
    assigned_sale = os.environ.get("ORBIS_TEST_MEXC_ASSIGNED_SALE") == "1"
    assert not assigned_sale or manual_sale_rearm
    manual_rearm = manual_sale_rearm or os.environ.get("ORBIS_TEST_MEXC_MANUAL_REARM") == "1"
    assert not (cash_rearm and manual_rearm)
    orders_before_rearm = 1 + int(manual_rearm) + int(manual_sale_rearm)
    orders_after_exit = orders_before_rearm + 1
    manual_review_amount = "0.0001 BTC fuera del bot" if manual_sale_rearm else "0.0002 BTC fuera del bot"
    allocation_confirmations = []
    def check_allocation_confirmation(count):
        assert len(allocation_confirmations) == count, allocation_confirmations
        message = allocation_confirmations[-1]
        if assigned_sale:
            amount = re.match(r"Venta manual atribuible al bot · (\S+) BTC\n", message)
            assert amount and Decimal(amount[1]) == Decimal("0.0001"), message
        else:
            value = manual_review_amount if manual_rearm else "10 USDT"
            assert value in message, message

    def confirm(dialog):
        if dialog.message.startswith(("Sólo cambió el efectivo", "Compra manual conciliada", "Venta manual conciliada", "Venta manual atribuible")):
            allocation_confirmations.append(dialog.message)
            if len(allocation_confirmations)==1:
                dialog.dismiss()
                return
        dialog.accept()
    page.on("dialog",confirm)
    stale_status = []
    page.route('**/real/exchanges/mexc/status', lambda route: stale_status.append(route))
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
    with page.expect_response('**/real/exchanges/mexc/connect') as connection:
        page.get_by_role("button",name="Consultar MEXC",exact=True).click()
    assert connection.value.status == 200, connection.value.text()
    expect(page.locator('#real-mexc-actions')).to_be_visible()
    assert len(stale_status) == 1
    # A late initial disconnected response must not hide a newer connection.
    stale_status[0].fulfill(status=200, json={'connected':False})
    page.wait_for_load_state('networkidle')
    expect(page.locator('#real-mexc-actions')).to_be_visible()
    page.unroute('**/real/exchanges/mexc/status')
    page.locator("#real-mexc-trade-verify > summary").click()
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("fixture-trade")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button",name="Verificar Trade",exact=True).click()
    expect(page.locator("#real-mexc-trade-state")).to_contain_text("autorización de operativa separada")
    panel = page.locator("#real-mexc-bots")
    page.evaluate("""() => {
      const original = window.LightweightCharts;
      window.LightweightCharts = Object.assign({},original,{createChart(host,options) {
        const chart = original.createChart(host,options);
        if (host.matches('[data-real-chart-canvas]')) {
          window.fixtureBotChart = chart;
          const remove = chart.remove;
          chart.remove = function() { window.fixtureChartRemoved = (window.fixtureChartRemoved || 0)+1; return remove.call(chart); };
        }
        return chart;
      }});
    }""")
    panel.locator("summary").click()
    expect(panel.locator('[name="paper"] option')).to_have_count(2)
    panel.locator('[name="paper"]').select_option(index=1)
    expect(panel.locator('[name="research"] option')).to_have_count(2)
    assert panel.locator('[name="research"]').input_value()
    panel.locator('[name="budget"]').fill("20")
    assert panel.locator('[name="draft"]').input_value()
    assert panel.locator("form").evaluate("form => form.checkValidity()")
    with page.expect_response(lambda response: "/mexc-bots/research-review?" in response.url) as reviewed:
        panel.get_by_role("button", name="Verificar origen", exact=True).click()
    assert reviewed.value.ok and reviewed.value.request.method == "GET", reviewed.value.text()
    expect(panel.locator("[data-bots-origin]")).to_contain_text("Última vela PAPER")
    expect(panel.locator("[data-bots-origin]")).to_contain_text("Reserva")
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 0
    with page.expect_response(lambda response:response.request.method == "POST" and response.url.endswith("/mexc-bots")) as activation:
        panel.get_by_role("button",name="Activar bot",exact=True).click()
    assert activation.value.status == 200, activation.value.text()
    research = activation.value.json()['research_binding']
    assert activation.value.request.post_data_json['research_review_digest'] == reviewed.value.json()['sha256'] == research['sha256']
    assert research['research']['artifact_id'] == panel.locator('[name="research"]').input_value()
    assert research['research']['execution_authority'] == 'NONE'
    expect(panel.locator('[data-bots-list]')).to_contain_text('Reserva · '+research['research']['artifact_id'][:8])
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 0
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    panel.get_by_role("button",name="Gráfico",exact=True).click()
    graph = panel.locator("[data-bots-chart]")
    expect(graph.locator("[data-real-chart-state]")).to_contain_text("0 fills")
    export_button = graph.get_by_role("button",name="Exportar JSONL",exact=True)
    expect(export_button).to_be_disabled()
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-fill-count","0")
    assert graph.locator("canvas").count() > 0
    assert page.evaluate("!!window.fixtureBotChart")
    assert page.request.post(BASE + "/__test/entry").json()["poll_result"] == "OBSERVED"
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 1",timeout=15000)
    expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(1)
    assert page.request.get(BASE + "/__test/posts").json()["count"] == 1
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-buy-count","1")
    last_fill = max(page.request.get(BASE+"/__test/posts").json()["fill_times"])//1000
    page.wait_for_function("time => window.fixtureBotChart.timeScale().getVisibleRange()?.to >= time",arg=last_fill)
    expect(graph.locator("[data-real-chart-inspect]")).to_contain_text("ENTRY_RULE")
    expect(graph.locator("[data-real-chart-inspect]")).to_contain_text("Asiento")
    assert graph.locator("[data-real-chart-canvas]").bounding_box()["height"] >= 420
    # Pausa con posición REAL abierta; el rearme no la sustituye por PAPER.
    panel.get_by_role("button",name="Detener bot",exact=True).click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 1")
    if cash_rearm:
        assert page.request.post(BASE+"/__test/add-cash").json()=={"cash_added":"10"}
    if manual_rearm:
        trading = page.locator("#real-mexc-trading")
        trading_summary = trading.locator(":scope > details > summary")
        page.get_by_role("button", name="Orden Spot Compra / venta", exact=True).click()
        expect(trading_summary).to_be_focused()
        trading.get_by_text("Configurar límites manuales", exact=True).click()
        limits = trading.locator("[data-trading-limits]")
        for name, value in {"base_asset":"BTC", "max_order_quote":"30", "max_daily_turnover_quote":"100",
                            "max_exposure_quote":"50", "max_daily_loss_quote":"5",
                            "max_fee_bps":"20", "max_slippage_bps":"30"}.items():
            limits.locator(f'[name="{name}"]').fill(value)
        limits.get_by_role("button", name="Guardar límites manuales").click()
        expect(trading.locator("[data-trading-limits-review]")).to_contain_text("Máx. por orden 30 USDT")
        trading.get_by_role("button", name="Revisar saldo", exact=True).click()
        with page.expect_response("**/mexc-policy-drafts/*/activate") as activated:
            trading.get_by_role("button", name="Habilitar operativa", exact=True).click()
        assert activated.value.ok and activated.value.json()["manual_only"], activated.value.text()
        def place_manual(side, quantity, price):
            trading.locator('[name="side"]').select_option(side)
            trading.locator('[name="quantity"]').fill(quantity)
            trading.locator('[name="price"]').fill(price)
            with page.expect_response("**/mexc-trading-sessions/*/orders") as execution:
                trading.get_by_role("button", name="Enviar orden real", exact=True).click()
            response = execution.value
            assert response.ok and response.json()["journal_status"] == "RESERVATION_CLOSED", response.text()
            expect(trading.locator('[name="quantity"]')).to_have_value("")

        place_manual("BUY", "0.0002", "65001")
        expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(2)
        if manual_sale_rearm:
            place_manual("SELL", "0.0003" if assigned_sale else "0.0001", "65000")
            expect(page.locator("#real-history").get_by_text("SELL", exact=True)).to_have_count(1)
        trading_summary.click()
    panel.get_by_role("button",name="Reanudar",exact=True).click()
    expect(panel.locator("[data-bots-mode]")).to_contain_text("Reanudar")
    assert panel.locator('[name="budget"]').input_value() == "20"
    if cash_rearm or manual_rearm:
        panel.get_by_role("button",name="Revisar y reanudar",exact=True).click()
        expect(panel.locator("[data-bots-state]")).to_have_text("Reanudación cancelada.")
        check_allocation_confirmation(1)
        assert page.request.get(BASE+"/__test/posts").json()["count"] == orders_before_rearm
        expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    try:
        with page.expect_response(lambda response:response.request.method == "POST" and response.url.endswith("/rearm")) as rearm:
            panel.get_by_role("button",name="Revisar y reanudar",exact=True).click()
    except BrowserTimeout:
        panel.screenshot(path="/tmp/orbis-bot-rearm-error.png")
        raise AssertionError(panel.inner_text()) from None
    assert rearm.value.status == 200, rearm.value.text()
    assert rearm.value.json()["position"]["fill_count"] == 1
    assert rearm.value.json()["reauthorizations"] == 1
    if cash_rearm:
        assert len(allocation_confirmations)==2
        assert len(rearm.value.request.post_data_json["allocation_review_digest"])==64
        (Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])/"result.json").write_text(json.dumps({
            "cash_added":"10","cancelled_first_review":True,"carried_fill_count":rearm.value.json()["position"]["fill_count"],
            "orders_at_rearm":page.request.get(BASE+"/__test/posts").json()["count"]}))
    if manual_rearm:
        check_allocation_confirmation(2)
        if assigned_sale:
            assert rearm.value.json()["position"]["manual_sale_count"] == 1
            assert "Ingreso neto atribuido" in allocation_confirmations[1]
            expect(panel.locator("[data-bots-list]")).to_contain_text("Fills de venta manual atribuidos: 1")
        elif manual_sale_rearm:
            amounts = re.search(r"Inventario manual ([0-9.]+) → ([0-9.]+) BTC", allocation_confirmations[1])
            assert amounts and tuple(map(Decimal, amounts.groups())) == (Decimal(0), Decimal("0.0001"))
        assert len(rearm.value.request.post_data_json["allocation_review_digest"]) == 64
    assert page.request.get(BASE + "/__test/posts").json()["count"] == orders_before_rearm
    expect(panel.locator("[data-bots-list]")).to_contain_text("Revisiones: 1")
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    page.request.post(BASE + "/__test/target")
    expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 2",timeout=15000)
    expect(page.locator("#real-history").get_by_text("SELL",exact=True)).to_have_count(1 + int(manual_sale_rearm))
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-fill-count","2")
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-sell-count","1")
    last_fill = max(page.request.get(BASE+"/__test/posts").json()["fill_times"])//1000
    page.wait_for_function("time => window.fixtureBotChart.timeScale().getVisibleRange()?.to >= time",arg=last_fill)
    expect(graph.locator("[data-real-chart-inspect]")).to_contain_text("TAKE_PROFIT")
    expect(graph.get_by_role("combobox",name="Inspeccionar fill REAL").locator("option")).to_have_count(3)
    graph.get_by_role("combobox",name="Inspeccionar fill REAL").select_option(index=1)
    expect(graph.locator("[data-real-chart-inspect]")).to_contain_text("Compra")
    candle_count = int(graph.locator("[data-real-chart-canvas]").get_attribute("data-candle-count"))
    graph.get_by_role("button",name="Histórico anterior",exact=True).click()
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-candle-count",str(candle_count+6))
    expect(graph.get_by_role("button",name="Histórico anterior",exact=True)).to_be_disabled()
    graph.get_by_role("button",name="Actualizar gráfico",exact=True).click()
    expect(graph.locator("[data-real-chart-state]")).to_contain_text("vela en formación")
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-candle-count",str(candle_count+6))
    graph.screenshot(path="/tmp/orbis-bot-real-fills.png")
    with page.expect_download() as downloaded:
        export_button.click()
    raw_export = Path(downloaded.value.path()).read_bytes()
    records = [json.loads(line) for line in raw_export.splitlines()]
    assert [row["fill"]["side"] for row in records] == ["BUY","SELL"]
    assert [row["provenance"]["authorization_index"] for row in records] == [1,2]
    assert all(row["provenance"]["reserve"]["artifact_id"] == research["research"]["artifact_id"] for row in records)
    assert page.locator("#real-account").input_value() not in raw_export.decode()
    expect(graph.locator("[data-real-chart-export-state]")).to_have_text("2 fills exportados")
    panel.get_by_role("button",name="Diario",exact=True).click()
    expect(panel.locator("[data-bot-journal]")).to_contain_text("BUY · ENTRY_RULE")
    expect(panel.locator("[data-bot-journal]")).to_contain_text("SELL · TAKE_PROFIT")
    panel.screenshot(path="/tmp/orbis-bot-real.png")
    before_mobile = page.evaluate("window.fixtureBotChart.timeScale().getVisibleRange()")
    page.set_viewport_size({"width":390,"height":844})
    page.wait_for_function("range => { const now = window.fixtureBotChart.timeScale().getVisibleRange(); return now && now.from <= range.from && now.to >= range.to; }",arg=before_mobile)
    graph.screenshot(path="/tmp/orbis-bot-real-fills-mobile.png")
    page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth + 1")
    overflow = page.evaluate("""() => Array.from(document.querySelectorAll('body *')).filter(item => {
      const rect = item.getBoundingClientRect(); return rect.width > 0 && rect.right > innerWidth+1;
    }).slice(0,25).map(item => ({tag:item.tagName,id:item.id,class:item.className,
      right:item.getBoundingClientRect().right,width:item.getBoundingClientRect().width,min:getComputedStyle(item).minWidth}))""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"),overflow
    panel.screenshot(path="/tmp/orbis-bot-active-mobile.png")
    page.set_viewport_size({"width":1440,"height":1000})
    page.get_by_role("button",name="Ocultar importes",exact=True).click()
    expect(page.get_by_role("button",name="Mostrar importes",exact=True)).to_be_visible()
    panel.locator("summary").click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Propio: ••••")
    panel.get_by_role("button",name="Gráfico",exact=True).click()
    expect(graph.locator("[data-real-chart-inspect]")).to_contain_text("••••")
    expect(graph.locator("[data-real-chart-inspect]")).not_to_contain_text("72000")
    expect(export_button).to_be_disabled()
    panel.get_by_role("button",name="Detener bot",exact=True).click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    result = page.request.get(BASE + "/__test/posts").json()
    assert result["count"] == orders_after_exit and all(not worker["running"] for worker in result["workers"])
    if manual_rearm:
        bot_bought, manual_bought, *sales = map(Decimal, result["fill_sizes"])
        bot_debit = Decimal("0.0001") if assigned_sale else Decimal(0)
        bot_residual = bot_bought - bot_debit - sales[-1]
        manual_sold = sales[0] if manual_sale_rearm else Decimal(0)
        manual_remaining = Decimal(result["venue_base"]) - bot_residual
        assert manual_bought == Decimal("0.0002")
        assert manual_sold == (Decimal("0.0003") if assigned_sale else Decimal("0.0001") if manual_sale_rearm else 0)
        assert manual_remaining == manual_bought - manual_sold + bot_debit
        assert Decimal(0) <= bot_residual <= Decimal("0.000001")
    page.get_by_role("button",name="Bloquear",exact=True).click()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    page.get_by_role("button",name="Mostrar importes",exact=True).click()
    expect(page.get_by_role("button",name="Ocultar importes",exact=True)).to_be_visible()
    panel.locator("summary").click()
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    if assigned_sale:
        expect(panel.locator("[data-bots-list]")).to_contain_text("Fills de venta manual atribuidos: 1")
    panel.get_by_role("button",name="Gráfico",exact=True).click()
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-fill-count","2")

    def wrong_digest(route):
        response = route.fetch()
        assert response.status == 200
        route.fulfill(json={**response.json(),"sha256":"0"*64})

    page.route("**/mexc-bots/*/research-export",wrong_digest,times=1)
    export_button.click()
    expect(graph.locator("[data-real-chart-export-state]")).to_have_text("La huella del archivo no coincide")
    with page.expect_download() as reopened:
        export_button.click()
    assert Path(reopened.value.path()).read_bytes() == raw_export

    def wrong_environment(route):
        response = route.fetch()
        payload = response.json()
        payload["environment"] = "PAPER"
        route.fulfill(status=200,json=payload)
    page.route("**/executions-page?*",wrong_environment,times=1)
    graph.get_by_role("button",name="Actualizar gráfico",exact=True).click()
    expect(graph.locator("[data-real-chart-state]")).to_contain_text("Gráfico no verificable")
    expect(graph.locator("canvas")).to_have_count(0)
    assert graph.locator("[data-real-chart-canvas]").get_attribute("data-fill-count") is None
    graph.get_by_role("button",name="Actualizar gráfico",exact=True).click()
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-fill-count","2")
    # Sin OHLC, se conservan sólo puntos de ejecución; jamás precios de otro proveedor.
    graph.get_by_role("button",name="Cerrar gráfico",exact=True).click()
    page.route("**/market/candles?provider=mexc&*",lambda route:route.fulfill(status=200,json={
        "available":True,"resolved_provider":"kraken","symbol":"BTC/USDT","timeframe":"1h","candles":[]}),times=1)
    panel.get_by_role("button",name="Gráfico",exact=True).click()
    expect(graph.locator("[data-real-chart-state]")).to_contain_text("0 velas")
    expect(graph.locator("[data-real-chart-state]")).to_contain_text("Fuente de velas distinta")
    expect(graph.locator("[data-real-chart-canvas]")).to_have_attribute("data-fill-count","2")
    expect(panel.get_by_role("button",name="Activar bot",exact=True)).to_be_disabled()
    assert page.request.get(BASE + "/__test/posts").json()["count"] == orders_after_exit
    page.set_viewport_size({"width":390,"height":844})
    page.screenshot(path="/tmp/orbis-bot-mobile.png",full_page=True)
    overflow = page.evaluate("""() => Array.from(document.querySelectorAll('body *')).filter(item => {
      const rect = item.getBoundingClientRect();
      return rect.width > 0 && rect.right > innerWidth + 1;
    }).slice(0,25).map(item => ({tag:item.tagName,id:item.id,class:item.className,
      right:item.getBoundingClientRect().right,width:item.getBoundingClientRect().width}))""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), overflow
    assert not errors, errors
    removed = page.evaluate("window.fixtureChartRemoved || 0")
    page.get_by_role("link",name="Inicio",exact=True).click()
    page.wait_for_function("count => (window.fixtureChartRemoved || 0) > count",arg=removed)
    expect(page.locator("[data-real-chart-canvas]")).to_have_count(0)
    assert page.request.get(BASE+"/__test/posts").json()["count"] == orders_after_exit
    if manual_rearm:
        (Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])/"result.json").write_text(json.dumps({
            "manual_buy_via_ui": True, "cancelled_first_review": True,
            "carried_fill_count": rearm.value.json()["position"]["fill_count"],
            "orders_at_rearm": orders_before_rearm, "orders_after_exit": result["count"],
            "manual_units_after_exit": str(manual_remaining), "bot_residual": str(bot_residual),
            "external_financial_transactions": 0,
            **({"assigned_units": str(bot_debit), "reopened_attribution": True} if assigned_sale else {}),
            **({"manual_sale_via_ui": True, "manual_units_sold": str(manual_sold)} if manual_sale_rearm else {}),
        }))
    browser.close()
    print("Bot UI: BUY → pausa → rearme → SELL; gráfico de fills REAL, inspección exacta, móvil, rechazo PAPER/fuente ajena y reapertura sin claves OK · exchange ficticio")
