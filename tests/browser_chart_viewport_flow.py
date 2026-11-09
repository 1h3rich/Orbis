"""La vista elegida del gráfico compartido sobrevive a un refresco.

Ejecutar con servidor aislado en 127.0.0.1:8765; precios interceptados.
"""

from playwright.sync_api import sync_playwright
from browser_paper_preview_mock import mock_preview
from urllib.parse import parse_qs, urlparse


BASE = "http://127.0.0.1:8765/ui/"
HOURLY = [{"t": 1_800_000_000 + index * 3600,
           "open": str(100 + index), "high": str(102 + index),
           "low": str(99 + index), "close": str(101 + index)}
          for index in range(24)]
DAILY = [{"t": 1_760_000_000 + index * 86400,
          "o": str(100 + index), "h": str(102 + index),
          "l": str(99 + index), "c": str(101 + index)}
         for index in range(366)]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/market/price?*", lambda route: route.fulfill(
        status=200, json={"price": "123", "source": "kraken",
                          "age_seconds": 1, "stale": False}))
    page.route("**/paper/market/preview?*", mock_preview)

    def history(route):
        timeframe = parse_qs(urlparse(route.request.url).query).get("timeframe", ["24h"])[0]
        route.fulfill(status=200, json={"points": HOURLY,
                                        "source": "kraken", "timeframe": timeframe})

    page.route("**/market/history?*", history)
    page.route("**/market/candles?*", lambda route: route.fulfill(
        status=200, json={"available": True, "resolved_provider": "kraken",
                          "candles": DAILY}))
    page.goto(BASE + "#/operativa")
    page.wait_for_load_state("networkidle")
    page.locator("#t-chart-cv canvas").first.wait_for(state="visible")
    installed = page.evaluate("""() => {
      window.__orbisCharts = [];
      const vendor = window.LightweightCharts;
      window.LightweightCharts = {...vendor, createChart: function(...args) {
        const chart = vendor.createChart(...args);
        window.__orbisCharts.push(chart);
        return chart;
      }};
      return window.LightweightCharts.createChart !== vendor.createChart;
    }""")
    assert installed, "No se pudo instrumentar Lightweight Charts"

    page.locator("#t-reload").click()
    page.wait_for_function("() => window.__orbisCharts.length >= 1")
    page.evaluate("window.__orbisCharts[0].timeScale().setVisibleLogicalRange({from: 5, to: 15})")
    page.locator("#t-reload").click()
    page.wait_for_function("() => window.__orbisCharts.length >= 2")
    panned = page.evaluate("window.__orbisCharts[1].timeScale().getVisibleLogicalRange()")
    assert panned and abs(panned["from"] - 5) < 1 and abs(panned["to"] - 15) < 1, panned

    page.evaluate("window.__orbisCharts[1].timeScale().setVisibleLogicalRange({from: 14, to: 26})")
    page.locator("#t-reload").click()
    page.wait_for_function("() => window.__orbisCharts.length >= 3")
    following = page.evaluate("window.__orbisCharts[2].timeScale().getVisibleLogicalRange()")
    assert following and abs(following["to"] - 26) < 1, following

    page.locator("#t-range").select_option("1y")
    page.wait_for_function("() => window.__orbisCharts.length >= 4")
    changed = page.evaluate("window.__orbisCharts[3].timeScale().getVisibleLogicalRange()")
    assert changed and changed["to"] > 300, changed
    height = page.locator("#t-chart-cv").bounding_box()["height"]
    assert height >= 600, height
    page.screenshot(path="/tmp/orbis-operativa-vista-persistente.png", full_page=True)
    browser.close()
    print("Vista gráfica: paneo, seguimiento derecho y cambio de rango OK")
