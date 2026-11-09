"""Real chart and encrypted LAB with a deterministic confirmed divergence."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.test_lab_rsi_divergence import divergence_bars, BULL

BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUTPUT = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])
ROWS = [bar.json() for bar in divergence_bars()]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
      window.labMarkers = [];
      Object.defineProperty(window, "LightweightCharts", {
        configurable: true,
        set(library) {
          const real = library.createSeriesMarkers;
          Object.defineProperty(window, "LightweightCharts", {
            value: {...library, createSeriesMarkers(series, markers) {
              window.labMarkers.push(markers || []);
              const api = real(series, markers);
              const setMarkers = api.setMarkers.bind(api);
              api.setMarkers = function (points) {
                window.labMarkers.push(points);
                return setMarkers(points);
              };
              return api;
            }}, configurable: true
          });
        }
      });
    })();""")
    page.goto(BASE + "/ui/#/config")
    page.locator("#cfg-privacidad").get_by_text("LAB activo", exact=True).click()
    create = page.locator("#cfg-lab-create-form")
    create.locator("[name=passphrase]").fill("isolated orbis divergence lab password")
    create.get_by_role("button", name="Crear LAB cifrado").click()
    page.locator("#cfg-lab-status").get_by_text("desbloqueado", exact=False).wait_for(timeout=30000)
    response = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "divergence-browser-fixture", "candles": ROWS,
    })
    assert response.ok, response.text()
    dataset_id = response.json()["id"]

    page.goto(BASE + "/ui/#/laboratorio")
    dataset = page.locator("#lab-dataset")
    dataset.select_option(dataset_id)
    leaf = page.locator("#lab-rule-list .lab-rule").first
    leaf.locator("[name=kind]").select_option(BULL)
    expect(leaf.locator("[name=value]")).to_be_hidden()
    expect(leaf.locator("[name=period]")).to_have_value("14")
    leaf.locator("[name=period]").fill("3")
    leaf.locator("[name=lookback_bars]").fill("8")
    page.locator("#lab-research-form [name=source]").select_option("editor")
    page.get_by_role("button", name="Buscar casos").click()
    page.get_by_text("Investigación guardada", exact=False).wait_for()
    research = page.locator("#lab-research-report")
    chart = research.locator("#lab-research-chart")
    expect(chart).to_have_attribute("data-signal-count", "1")
    case = research.locator("#lab-research-event-rows tr")
    expect(case).to_have_count(1)
    case.press("Enter")
    expect(research.locator("#lab-research-chart-detail")).to_contain_text("Divergencia RSI alcista")
    expect(research.locator("#lab-research-chart-detail")).to_contain_text("RSI ≈ 75")
    expect(research.locator("#lab-research-chart-detail")).to_contain_text("Confirma 2025-01-01 15:00 UTC")
    markers = page.evaluate("window.labMarkers.findLast(points => points.some(point => point.text === 'G2'))")
    assert [(point["time"], point["text"]) for point in markers] == [
        (ROWS[4]["t"], "G1"), (ROWS[12]["t"], "G2"), (ROWS[14]["t"], "Estructura"),
    ], markers
    research.locator(".lab-verify button").click()
    expect(research.locator(".lab-verify")).to_have_attribute("data-state", "verified")
    research.locator("#lab-research-chart").screenshot(path=str(OUTPUT / "divergencia-estructura.png"))
    research.locator("#lab-research-to-strategy").click()
    expect(leaf.locator("[name=kind]")).to_have_value(BULL)
    expect(leaf.locator("[name=lookback_bars]")).to_have_value("8")
    form = page.locator("#lab-strategy-form")
    form.locator("[name=name]").fill("Divergencia confirmada")
    for name, value in (("fee_rate", "0"), ("slippage_rate", "0"), ("max_holding_bars", "2")):
        form.locator(f"[name={name}]").fill(value)
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-run").click()
    page.locator("#lab-notice").get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-verify-action").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    page.wait_for_function("""() => window.labMarkers.some(points =>
      points.some(point => point.text === 'Señal') && points.some(point => point.text === 'Entrada'))""")
    backtest = page.evaluate("""window.labMarkers.findLast(points => points.some(point => point.text === 'Entrada'))
      .filter(point => point.text === 'Señal' || point.text === 'Entrada')""")
    assert [(point["time"], point["text"]) for point in backtest] == [
        (ROWS[14]["t"], "Señal"), (ROWS[15]["t"], "Entrada"),
    ]
    page.reload()
    page.locator("#lab-strategy").select_option(label="Divergencia confirmada · v1")
    expect(leaf.locator("[name=lookback_bars]")).to_have_value("8")
    expect(leaf.locator("[name=kind]")).to_have_value(BULL)
    expect(page.locator("#lab-research-chart")).to_have_attribute("data-signal-count", "1")
    assert not errors, errors
    (OUTPUT / "divergencia-result.json").write_text(json.dumps({
        "pivots": [point["time"] for point in markers[:2]],
        "signal": backtest[0]["time"], "entry": backtest[1]["time"],
        "saved_after_reload": True, "verification": "verified",
    }))
    browser.close()
    print("Divergencia RSI: giros, confirmación, entrada, verificación y reapertura correctas.")
