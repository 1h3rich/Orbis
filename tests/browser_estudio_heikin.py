"""Estudio muestra Heikin-Ashi sin sustituir los precios de mercado."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8765/ui/#/estudio"
FIRST = 1_800_000_000
ROWS = [
    {"t": FIRST, "o": "10", "h": "14", "l": "8", "c": "12", "v": "5", "closed": True},
    {"t": FIRST + 3600, "o": "12", "h": "20", "l": "10", "c": "18", "v": "8", "closed": True},
    {"t": FIRST + 7200, "o": "18", "h": "22", "l": "17", "c": "20", "v": "9", "closed": True},
]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
      let library;
      Object.defineProperty(window, 'LightweightCharts', {
        configurable: true,
        get() { return library; },
        set(value) {
          library = Object.assign({}, value);
          library.createChart = function (host, options) {
            const chart = value.createChart(host, options);
            if (host.id !== 'st-chart') return chart;
            const add = chart.addSeries.bind(chart);
            chart.addSeries = function (kind, config, pane) {
              const series = add(kind, config, pane);
              if (kind === value.CandlestickSeries) {
                const setData = series.setData.bind(series);
                series.setData = function (rows) {
                  window.__orbisHeikinRows = rows;
                  return setData(rows);
                };
              }
              return series;
            };
            return chart;
          };
        }
      });
    })()""")
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": True, "symbol": "BTC/EUR", "resolved_provider": "kraken",
        "timeframe": "1h", "older_pagination_supported": False, "candles": ROWS,
    }))
    page.route("**/market/snapshot?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "kraken", "price": "20",
        "timestamp": ROWS[-1]["t"],
    }))
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    page.locator("#st-tf").select_option("1h")
    page.locator("#st-ctype").select_option("heikin")
    page.wait_for_function("() => window.__orbisHeikinRows?.length === 3 && window.__orbisHeikinRows[1].close === 15")
    assert page.evaluate("window.__orbisHeikinRows[0]") == {
        "time": FIRST, "open": 11, "high": 14, "low": 8, "close": 11,
    }
    expect(page.locator("#st-ctype")).to_have_value("heikin")
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#st-ctype")).to_have_value("heikin")
    page.wait_for_function("() => window.__orbisHeikinRows?.length === 3 && window.__orbisHeikinRows[1].close === 15")
    assert not errors, errors
    browser.close()
    print("Estudio: Heikin-Ashi calculado y persistente, histórico original intacto")
