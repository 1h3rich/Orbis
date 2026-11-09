"""Recorrido UI → API → SQLite temporal → UI para PAPER manual GRVT."""

from urllib.parse import parse_qs, urlparse
import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get('ORBIS_TEST_BASE_URL',"http://127.0.0.1:8765")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    errors = []
    clock = {"seconds": 1_800_001_800, "closed_only": True}

    def market_candles(route):
        query = parse_qs(urlparse(route.request.url).query)
        symbol = query.get("symbol", ["BTC/USDT:PERP"])[0]
        timeframe = query.get("timeframe", ["1h"])[0]
        step = {"1h": 3600, "4h": 14400, "1d": 86400}.get(timeframe, 3600)
        last = clock["seconds"] // step * step - (step if clock["closed_only"] else 0)
        candles = [{"t": last - index * step, "o": "100", "h": "112",
                    "l": "95", "c": "105", "v": "10"} for index in range(99, -1, -1)]
        route.fulfill(status=200, json={"symbol": symbol, "resolved_provider": "grvt",
                                        "timeframe": timeframe, "available": True,
                                        "candles": candles})

    page.route("**/market/candles?*", market_candles)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/perpetuos")
    page.wait_for_load_state("networkidle")
    page.locator("#perp-account-form").wait_for()
    expect(page.locator("#perp-instrument-spec")).to_contain_text("mín. 0.01 BTC")
    page.screenshot(path="/tmp/orbis-perp-before.png", full_page=True)
    assert page.get_by_role("heading", name="Perpetuos PAPER").count() == 1
    assert "Sin cuenta ficticia" in page.locator("#perp-summary").inner_text()
    created_vault = page.request.post(BASE + "/paper/perp/create", data={
        "passphrase": "orbis paper browser temporary password"})
    assert created_vault.ok, created_vault.text()

    page.locator("#perp-account-form [name=fee_rate]").fill("0.001")
    page.locator("#perp-account-form [name=slippage_bps]").fill("0")
    page.locator("#perp-account-form button[type=submit]").click()
    page.get_by_text("Cuenta PAPER creada", exact=False).wait_for()
    expect(page.locator("#perp-summary")).to_contain_text("10000 USDT")
    page.locator("#perp-open-form [name=symbol]").select_option("BTC/USDT:PERP")
    page.locator("#perp-open-form [name=size]").fill("0.001")
    page.locator("#perp-open-form button[type=submit]").click()
    expect(page.locator("#perp-message")).to_contain_text("Cantidad inferior al mínimo GRVT")
    assert "Todavía no hay posiciones" in page.locator("#perp-positions").inner_text()
    page.locator("#perp-open-form [name=size]").fill("1")
    page.locator("#perp-open-form [name=leverage]").fill("2")
    page.locator("#perp-open-form button[type=submit]").click()
    page.get_by_text("Posición PAPER abierta", exact=False).wait_for()
    expect(page.locator("#perp-positions")).to_contain_text("101")
    assert "LONG" in page.locator("#perp-positions").inner_text()
    expect(page.locator("#perp-chart")).to_be_visible()
    expect(page.locator("#perp-chart")).to_have_attribute("data-paper-markers", "1")
    expect(page.locator("#perp-chart-note")).to_contain_text("1 operación sin OHLC aún")
    page.screenshot(path="/tmp/orbis-perp-pending-action.png", full_page=True)
    assert page.locator("#perp-chart").bounding_box()["height"] >= 500
    page.locator("#perp-chart-range select").select_option("1")
    expect(page.locator("#perp-chart-range")).to_have_attribute("data-view", "1")
    expect(page.locator("#perp-chart-range")).to_contain_text("Cobertura parcial")
    assert page.locator("#perp-chart-tf").input_value() == "1d"
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#perp-chart-range")).to_have_attribute("data-view", "1")
    page.locator("#perp-chart-tf").select_option("1h")
    clock["closed_only"] = False

    def observed_signal(route):
        route.fulfill(status=200, json={"symbol": "BTC/USDT:PERP", "timeframe": "1h",
                                        "source": "GRVT_PAPER_OBSERVED_SIGNALS",
                                        "execution": "SIGNALS_SEPARATE_FROM_PAPER_EVENTS", "has_more": False,
                                        "markers": [{"session_id": "session", "version_id": "v1",
                                                     "candle_t": 1_800_000_000, "direction": "LONG",
                                                     "entry_match": True, "exit_match": False}]})

    page.route("**/paper/perp/signals/chart-markers-page?*", observed_signal)
    page.locator("#perp-chart-refresh").click()
    expect(page.locator("#perp-chart")).to_have_attribute("data-signal-markers", "1")
    expect(page.locator("#perp-chart-note")).to_contain_text("1 señal")
    page.unroute("**/paper/perp/signals/chart-markers-page?*")

    account_before_funding = page.locator("#perp-summary").inner_text()
    response = page.request.post(BASE + "/__test/advance", data={"hours": 2})
    assert response.ok
    clock["seconds"] += 2 * 3600
    page.locator('[data-perp-action="funding"]').click()
    expect(page.locator("#perp-funding-detail")).to_contain_text("Coste estimado 0.02 USDT")
    expect(page.locator("#perp-funding-detail")).to_contain_text("no contabilizada")
    assert page.locator("#perp-summary").inner_text() == account_before_funding
    page.locator("#perp-funding-detail summary").click()
    assert page.locator("#perp-funding-detail tbody tr").count() == 2
    page.screenshot(path="/tmp/orbis-perp-funding-estimate.png", full_page=True)
    page.route("**/funding-estimate", lambda route: route.fulfill(
        status=503, json={"detail": "Histórico incompleto"}))
    page.locator('[data-perp-action="funding"]').click()
    expect(page.locator("#perp-funding-detail")).to_contain_text(
        "Estimación no disponible: Histórico incompleto")
    expect(page.locator("#perp-funding-detail")).to_contain_text("No se ha supuesto un coste cero")
    page.unroute("**/funding-estimate")

    response = page.request.post(BASE + "/__test/quote", data={"bid":"109","ask":"111","mark":"110"})
    assert response.ok
    page.locator('[data-perp-action="observe"]').click()
    page.get_by_text("Valoración guardada", exact=False).wait_for()
    expect(page.locator("#perp-positions")).to_contain_text("9 USDT")
    expect(page.locator("#perp-positions")).to_contain_text("Umbral ref.")
    assert page.request.post(BASE + "/__test/advance", data={"seconds": 14}).ok
    page.locator("#perp-refresh").click()
    expect(page.locator("#perp-positions")).to_contain_text("9 USDT")
    assert page.request.post(BASE + "/__test/advance", data={"seconds": 2}).ok
    expect(page.locator("#perp-positions")).to_contain_text("Valoración caducada", timeout=5000)
    expect(page.locator("#perp-positions")).not_to_contain_text("P/L precio 9 USDT")
    assert page.locator("#perp-positions tbody tr td").nth(5).inner_text() == "—"
    response = page.request.post(BASE + "/__test/quote", data={"bid":"49","ask":"51","mark":"50"})
    assert response.ok
    page.locator('[data-perp-action="observe"]').click()
    page.get_by_text("Riesgo de mantenimiento", exact=False).wait_for()
    expect(page.locator("#perp-positions")).to_contain_text("Bajo mantenimiento")
    assert page.request.post(BASE + "/__test/advance", data={"seconds": 16}).ok
    page.locator("#perp-refresh").click()
    expect(page.locator("#perp-positions")).to_contain_text("Último riesgo de margen · caducado")
    expect(page.locator("#perp-positions")).not_to_contain_text("⚠ Bajo mantenimiento")
    assert "Not Found" not in page.locator(".perp-signals").inner_text()
    page.screenshot(path="/tmp/orbis-perp-maintenance-risk.png", full_page=True)
    response = page.request.post(BASE + "/__test/quote", data={"bid":"109","ask":"111","mark":"110"})
    assert response.ok
    page.locator('[data-perp-action="close"]').click()
    page.get_by_text("Posición PAPER cerrada", exact=False).wait_for()
    expect(page.locator("#perp-positions")).to_contain_text("Cerrada")
    expect(page.locator("#perp-chart")).to_have_attribute("data-paper-markers", "2")
    expect(page.locator("#perp-summary")).to_contain_text("7.79 USDT")
    page.locator('[data-perp-action="funding"]').click()
    expect(page.locator("#perp-funding-detail")).to_contain_text("Coste estimado 0.02 USDT")
    expect(page.locator("#perp-summary")).to_contain_text("7.79 USDT")
    page.locator('[data-perp-action="settle-funding"]').click()
    expect(page.locator("#perp-message")).to_contain_text("2 liquidaciones nuevas · 0.02 USDT")
    expect(page.locator("#perp-summary")).to_contain_text("7.77 USDT")
    expect(page.locator("#perp-positions")).to_contain_text("funding cubierto")
    expect(page.locator("#perp-events")).to_contain_text("FUNDING_PAPER")
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#perp-positions")).to_contain_text("Cerrada")
    expect(page.locator("#perp-instrument-spec")).to_contain_text("paso 0.001")
    expect(page.locator("#perp-summary")).to_contain_text("10007.77 USDT")
    page.screenshot(path="/tmp/orbis-perp-after.png", full_page=True)
    assert not errors, errors
    print("PAPER PERP UI: cuenta → LONG → valoración → cierre → recarga OK")
    browser.close()
