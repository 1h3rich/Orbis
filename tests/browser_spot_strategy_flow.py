"""Recorrido navegador de una estrategia LONG spot con capital ficticio."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8767")
START = 1_735_689_600
STEP = 3600

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(BASE + "/") else route.abort())
    encrypted = page.request.post(BASE + "/paper/spot/strategies/create", data={
        "passphrase": "isolated browser spot paper password"
    })
    assert encrypted.status == 200, encrypted.text()
    page.goto(BASE + "/ui/#/spot-paper")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#spot-paper-worker")).to_contain_text("ACTIVO")
    expect(page.locator("#spot-paper-strategy")).to_contain_text("Tres velas LONG")
    expect(page.locator("#spot-paper-dataset")).to_contain_text("BTC/USDT")
    expect(page.locator("#spot-paper-version")).to_contain_text("v1")
    reference = page.request.post(BASE + "/lab/runs", data={
        "dataset_id": page.locator("#spot-paper-dataset").input_value(),
        "version_id": page.locator("#spot-paper-version").input_value(),
    })
    assert reference.status == 200, reference.text()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#spot-paper-start").click()
    expect(page.locator("#spot-paper-notice")).to_contain_text("Sesión ficticia activada")
    expect(page.locator("#spot-paper-detail")).to_contain_text("Caja ficticia")
    page.evaluate("window.__oldDiary = document.querySelector('#spot-paper-event-rows'); "
                  "window.__oldStatus = document.querySelector('.spot-paper-status')")
    page.locator("#spot-paper-session").dispatch_event("change")
    page.wait_for_function("() => document.querySelector('.spot-paper-status') !== window.__oldStatus")
    assert page.evaluate("document.querySelector('#spot-paper-event-rows') === window.__oldDiary")
    expect(page.locator("#spot-paper-comparison")).to_contain_text("HISTÓRICO VERIFICADO")
    expect(page.locator("#spot-paper-comparison .paper-signal-replay")).to_contain_text("Sin velas PAPER para verificar")
    expect(page.locator("#spot-paper-comparison")).to_contain_text("no de rentabilidad")
    expect(page.locator("#spot-paper-export")).to_be_disabled()
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-buy-count", "0")
    range_control = page.locator("#spot-paper-range-control")
    for choice in ("1", "5", "10", "all"):
        range_control.locator("select").select_option(choice)
        expect(range_control.locator("[data-range-coverage]")).to_contain_text("Cobertura")
    range_control.locator("select").select_option("last:100")
    expect(range_control.locator("[data-range-coverage]")).to_contain_text("Tramo visible")
    range_control.locator("select").select_option("last:500")
    expect(range_control.locator("select")).to_have_value("last:500")
    range_control.locator("select").select_option("10")
    expect(range_control.locator("[data-range-coverage]")).to_contain_text("Cobertura parcial")
    range_control.locator("select").select_option("custom")
    expect(range_control.locator("[data-range-dates]")).to_be_visible()
    expect(range_control.locator("select")).to_have_value("10")
    expect(range_control.locator("[data-range-coverage]")).to_contain_text("Cobertura parcial")
    assert range_control.locator("[data-range-from]").input_value() == "2025-01-01"
    range_control.locator("[data-range-to]").fill("2024-12-31")
    range_control.locator("[data-range-apply]").click()
    expect(range_control.locator("[data-range-error]")).to_contain_text("en orden")
    range_control.locator("[data-range-from]").fill("2024-01-01")
    range_control.locator("[data-range-to]").fill("2024-01-02")
    range_control.locator("[data-range-apply]").click()
    expect(range_control.locator("[data-range-error]")).to_contain_text("No hay velas ni operaciones")
    expect(range_control.locator("[data-range-coverage]")).to_contain_text("Cobertura parcial")
    range_control.locator("[data-range-from]").fill("2025-01-01")
    range_control.locator("[data-range-to]").fill("2025-01-01")
    range_control.locator("[data-range-apply]").click()
    expect(range_control.locator("[data-range-coverage]")).to_contain_text("Visible: 2025-01-01")
    range_control.locator("[data-range-edit]").click()
    range_control.locator("[data-range-from]").fill("2024-01-01")
    range_control.locator("[data-range-cancel]").click()
    assert range_control.locator("[data-range-from]").input_value() == "2025-01-01"
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(range_control.locator("select")).to_have_value("custom")
    expect(range_control.locator("[data-range-coverage]")).to_contain_text("Cobertura de velas")
    expect(range_control.locator("[data-range-dates]")).to_be_hidden()
    range_control.locator("select").select_option("10")
    assert "SIN ÓRDENES" in page.locator(".spot-paper-page").inner_text()
    page.evaluate("""() => {
      const original = window.LightweightCharts;
      window.LightweightCharts = Object.assign({}, original, {
        createChart(host, options) {
          const chart = original.createChart(host, options);
          if (host.id === 'spot-paper-chart') window.paperChart = {chart, markers:[]};
          return chart;
        },
        createSeriesMarkers(series, markers, options) {
          window.paperChart.markers.push({data:series.data(), markers});
          return original.createSeriesMarkers(series, markers, options);
        },
      });
    }""")

    session_id = page.locator("#spot-paper-session").input_value()
    page.request.post(BASE + "/__test/advance_spot", data={"now": START + 4 * STEP + 1})
    page.wait_for_function("async id => { const r = await fetch('/paper/spot/strategies/' + id); return (await r.json()).events.length === 1; }", arg=session_id)
    page.locator("#spot-paper-poll").click()
    expect(page.locator("#spot-paper-detail")).to_contain_text("BUY")
    first_chart = page.request.get(BASE + "/paper/spot/strategies/" + session_id + "/chart").json()
    assert [(mark["kind"], mark["time"]) for mark in first_chart["markers"][-2:]] == [
        ("SIGNAL", START + 3 * STEP), ("BUY", START + 4 * STEP)]
    buy = first_chart["markers"][-1]
    assert buy["pending_candle"] is True and buy["execution_time"] == START + 4 * STEP + 1
    expect(page.locator("#spot-paper-chart-status")).to_contain_text("1 ejecución sin OHLC")
    plot = page.evaluate("window.paperChart.markers")
    signal_plot, buy_plot = plot
    assert signal_plot["markers"][-1]["time"] == START + 3 * STEP
    assert all(bar["time"] < buy["time"] for bar in signal_plot["data"])
    assert buy_plot["data"] == [{"time": buy["execution_time"], "value": float(buy["price"])}]
    assert buy_plot["markers"][0]["time"] == buy["execution_time"]
    chart = page.locator("#spot-paper-chart")
    chart.scroll_into_view_if_needed()
    page.wait_for_function("t => window.paperChart.chart.timeScale().timeToCoordinate(t) > 0",
                           arg=buy["execution_time"])
    execution_x = page.evaluate("t => window.paperChart.chart.timeScale().timeToCoordinate(t)", buy["execution_time"])
    box = chart.bounding_box()
    page.mouse.click(box["x"] + execution_x, box["y"] + 55)
    inspector = page.locator("#spot-paper-inspector")
    expect(inspector).to_contain_text("Compra PAPER · 100 USDT")
    expect(inspector).to_contain_text("2025-01-01 04:00:01.000000000 UTC")
    expect(inspector).to_contain_text("Bid 99 · Ask 100")
    inspector.get_by_role("button", name="Ver reglas").click()
    expect(inspector).to_contain_text("Entrada ficticia")
    expect(inspector).to_contain_text("Grupo Y")
    inspector.get_by_role("button", name="Ver ejecución").click()
    expect(inspector).to_contain_text("2025-01-01 04:00:01.000000000 UTC")
    expect(page.locator("#spot-paper-comparison")).to_contain_text("1 compras · 0 ventas")
    expect(page.locator("#spot-paper-comparison .paper-signal-replay")).to_contain_text("Señales verificadas · 1 vela")
    expect(page.locator("#spot-paper-comparison .paper-frequency")).to_contain_text("Señales / 100 velas")
    expect(page.locator("#spot-paper-comparison .paper-frequency")).to_contain_text("muestra PAPER escasa")
    expect(page.locator("#spot-paper-comparison .paper-frequency-scope")).to_have_count(2)
    expect(page.locator("#spot-paper-comparison .paper-frequency-scope").nth(0)).to_have_attribute(
        "data-help", re.compile(r"LAB · velas evaluadas del .* UTC al .* UTC"))
    expect(page.locator("#spot-paper-comparison .paper-frequency-scope").nth(1)).to_have_attribute(
        "data-help", re.compile(r"PAPER · velas evaluadas del .* UTC al .* UTC"))
    def clustered_frequency(route):
        response = route.fetch()
        body = response.json()
        body["signal_frequency"]["paper"]["signal_bursts"] = 1
        body["signal_frequency"]["paper_clustered"] = True
        route.fulfill(response=response, json=body)
    compare_url = BASE + "/paper/spot/strategies/" + session_id + "/comparison"
    page.route(compare_url, clustered_frequency)
    page.locator("#spot-paper-session").dispatch_event("change")
    expect(page.locator("#spot-paper-comparison .paper-frequency")).to_contain_text("PAPER: 1 racha")
    page.unroute(compare_url, clustered_frequency)
    expect(page.locator("#spot-paper-detail")).to_contain_text("≈ 900")
    expect(page.locator("#spot-paper-detail")).to_contain_text("Patrimonio si cierra")
    expect(page.locator("#spot-paper-detail")).to_contain_text("Abierto si cierra")
    def stale_detail(route):
        response = route.fetch()
        item = response.json()
        item["valuation"] = {**item["valuation"], "basis": "UNAVAILABLE_STALE_QUOTE",
                             "equity_if_closed": None, "unrealized_pnl_if_closed": None}
        route.fulfill(response=response, json=item)
    detail_url = BASE + "/paper/spot/strategies/" + session_id + "/summary"
    page.route(detail_url, stale_detail)
    page.locator("#spot-paper-session").dispatch_event("change")
    expect(page.locator("#spot-paper-detail")).to_contain_text("Cotización caducada")
    expect(page.locator("#spot-paper-detail .spot-paper-metrics")).to_contain_text("— USDT")
    page.unroute(detail_url, stale_detail)
    page.locator("#spot-paper-session").dispatch_event("change")
    expect(page.locator("#spot-paper-detail")).not_to_contain_text("Cotización caducada")
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-buy-count", "1")
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-signal-count", "1")
    page.locator("#spot-paper-detail button[data-inspect-time]").first.click()
    expect(page.locator("#spot-paper-inspector")).to_contain_text("Entrada ficticia")
    expect(page.locator("#spot-paper-inspector")).to_contain_text("Grupo Y")
    expect(page.locator("#spot-paper-inspector")).to_contain_text("Velas bajistas seguidas (3)")
    expect(page.locator("#spot-paper-inspector")).to_contain_text("Subidas de volumen · legado (3)")
    expect(page.locator("#spot-paper-inspector")).to_contain_text("3 / umbral 3")
    inspector.get_by_role("button", name="Ver ejecución").click()

    page.request.post(BASE + "/__test/advance_spot", data={"now": START + 5 * STEP + 1,
                                                           "bid": "110", "ask": "111",
                                                           "continuous": True}, timeout=120000)
    page.wait_for_function("async id => { const r = await fetch('/paper/spot/strategies/' + id); return (await r.json()).events.length === 2; }", arg=session_id)
    page.locator("#spot-paper-poll").click()
    expect(page.locator("#spot-paper-detail")).to_contain_text("SELL")
    expect(page.locator("#spot-paper-comparison")).to_contain_text("1 compras · 1 ventas")
    expect(page.locator("#spot-paper-detail")).to_contain_text("Objetivo")
    expect(page.locator("#spot-paper-detail")).not_to_contain_text("Abierto si cierra")
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-sell-count", "1")
    second_chart = page.request.get(BASE + "/paper/spot/strategies/" + session_id + "/chart").json()
    assert second_chart["markers"][-1]["kind"] == "SELL"
    assert second_chart["markers"][-1]["time"] == START + 5 * STEP
    assert second_chart["markers"][-1]["execution_time"] == START + 5 * STEP + 1
    assert second_chart["markers"][2]["pending_candle"] is False
    assert page.evaluate("window.paperChart.markers[1].data") == buy_plot["data"]
    expect(inspector).to_contain_text("2025-01-01 04:00:01.000000000 UTC")
    expect(inspector).not_to_contain_text("sin OHLC")
    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-signalskipped-count", "1")
    expect(page.locator("#spot-paper-detail")).to_contain_text("SEÑAL SIN ENTRADA")
    page.locator("#spot-paper-detail button[data-inspect-time]").first.click()
    expect(page.locator("#spot-paper-inspector")).to_contain_text("Entrada descartada; salida ficticia")
    expect(page.locator("#spot-paper-inspector")).to_contain_text("SELL PAPER")
    reserve_dataset = page.request.get(BASE + "/__test/spot_reserve_dataset").json()["id"]
    validation = page.request.post(BASE + "/lab/rolling-validations", data={
        "dataset_id": reserve_dataset, "version_id": page.locator("#spot-paper-version").input_value(), "folds": 3,
    })
    assert validation.ok, validation.text()
    validation_id = validation.json()["id"]
    reserve_panel = page.locator("#spot-paper-reserve")

    def check_reserve():
        reserve_panel.locator("summary").click()
        expect(reserve_panel.locator("select")).to_have_value(validation_id)
        with page.expect_response("**/reserve-comparison?validation_id=*") as compared:
            reserve_panel.get_by_role("button",name="Comparar",exact=True).click()
        response = compared.value
        assert response.ok and response.headers["cache-control"] == "no-store",response.text()
        report = response.json()
        assert report["execution_authority"] == "NONE" and len(report["reserves"]) == 3
        assert report["paper"]["metrics"]["closed_trades"] == 1
        expect(reserve_panel.get_by_role("table")).to_be_visible()
        expect(reserve_panel.get_by_role("columnheader",name="LAB · Reserva 3")).to_be_visible()
        expect(reserve_panel.get_by_role("status")).to_contain_text("Periodos y ejecución distintos")
        return report

    initial_report = check_reserve()
    reserve_panel.locator("summary").click()
    export = page.locator("#spot-paper-export")
    research_path = "**/research-export"
    export_requests = []
    page.on("request",lambda request:export_requests.append(request.method)
            if request.url.endswith("/research-export") else None)
    page.once("dialog",lambda dialog:dialog.dismiss())
    with page.expect_response(research_path):
        export.click()
    expect(export).to_be_enabled()
    assert export_requests == ["GET"]
    page.once("dialog",lambda dialog:dialog.accept())
    with page.expect_download() as downloaded:
        with page.expect_response(lambda response:response.request.method=="POST" and
                                  response.url.endswith("/research-export")) as accepted:
            export.click()
    response = accepted.value
    assert response.ok and response.headers["cache-control"] == "no-store"
    expected = response.json()
    raw = downloaded.value.path().read_bytes()
    assert sha256(raw).hexdigest() == expected["sha256"]
    assert downloaded.value.suggested_filename == f"orbis-spot-paper-{expected['sha256'][:12]}.jsonl"
    assert expected["event_count"] == 2 and raw.decode() == expected["jsonl"]
    header,buy,sell = [json.loads(line) for line in raw.splitlines()]
    assert header["kind"] == "HEADER" and header["environment"] == "PAPER"
    assert buy["outcome"]["execution"]["episode"] == sell["outcome"]["execution"]["episode"] == 1
    assert sell["outcome"]["entry_decision"] == "SKIPPED_POSITION_OPEN"
    assert buy["input"]["quote"]["ask"] == "100" and sell["input"]["quote"]["bid"] == "110"
    expect(page.locator("#spot-paper-export-state")).to_have_text("2 eventos exportados")
    assert export_requests == ["GET","GET","POST"]

    if os.environ.get("ORBIS_BROWSER_SCREENSHOT"):
        page.screenshot(path=os.environ["ORBIS_BROWSER_SCREENSHOT"], full_page=True)
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(range_control.locator("select")).to_have_value("10")
    expect(page.locator("#spot-paper-detail")).to_contain_text("SELL")
    assert check_reserve() == initial_report
    reopened = page.request.get(BASE+f"/paper/spot/strategies/{session_id}/research-export").json()
    assert reopened["revision"] == expected["revision"]
    if os.environ.get("ORBIS_TEST_EXECUTION_DIRECTORY"):
        export_proof = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])/"spot-research-export.json"
        export_proof.write_text(json.dumps({"verified":True,"rows":3,"events":2,
            "cancelled_without_post":True,"revision_after_reload":reopened["revision"],
            "file_sha256":expected["sha256"]}))

    reserve_panel.locator("summary").click()
    if os.environ.get("ORBIS_TEST_EXECUTION_DIRECTORY"):
        output = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])
        (output / "spot-reserve-result.json").write_text(json.dumps({
            "verified": True, "windows": 3, "snapshot_reproduced_after_reload": True,
            "paper_closed_trades": initial_report["paper"]["metrics"]["closed_trades"],
        }))

    expect(page.locator("#spot-paper-chart")).to_have_attribute("data-sell-count", "1")
    page.locator("#spot-paper-detail button[data-inspect-time]").first.click()
    expect(page.locator("#spot-paper-inspector")).to_contain_text("Entrada descartada; salida ficticia")
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#spot-paper-detail")).to_contain_text("SELL")
    page.locator("#spot-paper-detail").get_by_role("button", name="Ver ejecución").first.click()
    expect(inspector).to_contain_text("Venta PAPER · 110 USDT")
    expect(inspector).to_contain_text("2025-01-01 05:00:01.000000000 UTC")
    inspector.get_by_role("button", name="Ver reglas").click()
    expect(inspector).to_contain_text("Entrada descartada; salida ficticia")
    range_control.locator("select").select_option("custom")
    popup = range_control.locator("[data-range-dates]").bounding_box()
    assert popup["x"] >= 0 and popup["x"] + popup["width"] <= 390
    range_control.locator("[data-range-cancel]").click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    print("LAB → Spot PAPER: ejecución en el segundo y precio archivados, enlace a reglas, recarga y móvil OK")
    browser.close()
