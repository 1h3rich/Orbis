"""Altura útil y redimensionado de las gráficas Inicio/Operativa.

Mercado controlado para comprobar sólo la UI, sin consultar precios externos.
"""

import os

from playwright.sync_api import expect, sync_playwright
from browser_paper_preview_mock import mock_preview
from urllib.parse import parse_qs, urlparse


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8765/ui/")
POINTS = [{"t": 1_800_000_000 + hour * 3600,
           "open": str(100 + hour), "high": str(102 + hour),
           "low": str(99 + hour), "close": str(101 + hour)} for hour in range(24)]
POINTS_7D = [{"t": 1_800_000_000 + day * 86400,
              "open": str(100 + day), "high": str(102 + day),
              "low": str(99 + day), "close": str(101 + day)} for day in range(8)]
DAILY = [{"t": 1_740_000_000 + day * 86400, "o": str(100 + day),
          "h": str(102 + day), "l": str(99 + day), "c": str(101 + day)}
         for day in range(366)]


def configure(page, *, fallback=False):
    page.route("**/market/price?*", lambda route: route.fulfill(
        status=200, json={"price": "123", "source": "kraken", "age_seconds": 1, "stale": False}))
    page.route("**/paper/market/preview?*", mock_preview)
    def history(route):
        timeframe = parse_qs(urlparse(route.request.url).query).get("timeframe", ["24h"])[0]
        route.fulfill(status=200, json={"points": POINTS_7D if timeframe == "7d" else POINTS,
                                        "source": "kraken"})
    page.route("**/market/history?*", history)
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "kraken", "candles": DAILY}))
    page.route("**/market/btc-daily-archive?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "bitstamp", "candles": DAILY}))
    if fallback:
        page.route("**/ui/vendor/lightweight-charts.standalone.production.js*",
                   lambda route: route.fulfill(status=200, body="", content_type="application/javascript"))


def check_chart(page, path, chart_id, min_height, screenshot):
    page.goto(BASE + path)
    page.wait_for_load_state("networkidle")
    chart = page.locator("#" + chart_id)
    chart.wait_for(state="visible")
    box = chart.bounding_box()
    assert box and box["height"] >= min_height, (path, box)
    assert box["width"] > 250, (path, box)
    assert page.locator("#" + chart_id + " canvas").count() or chart.evaluate("node => node.tagName") == "CANVAS"
    assert chart.evaluate("node => node.scrollWidth <= node.clientWidth + 2")
    range_text = page.locator('[data-chart-range="' + chart_id + '"]').inner_text()
    assert "2027" in range_text, range_text
    page.screenshot(path=screenshot, full_page=True)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    desktop = browser.new_page(viewport={"width": 1440, "height": 900})
    configure(desktop)
    check_chart(desktop, "#/inicio", "home-chart-cv", 500, "/tmp/orbis-inicio-chart-large.png")
    desktop.locator("#home-range").select_option("5y")
    expect(desktop.locator("#home-market .amber")).to_contain_text("Cobertura limitada")
    expect(desktop.locator('[data-chart-range="home-chart-cv"]')).to_contain_text("2025")
    check_chart(desktop, "#/operativa", "t-chart-cv", 500, "/tmp/orbis-operativa-chart-large.png")
    header = desktop.locator(".operativa-cabecera").bounding_box()
    chart = desktop.locator("#t-chart-cv").bounding_box()
    assert header and chart and chart["y"] - header["y"] < 340, (header, chart)
    assert desktop.locator("#op-refresh-now").is_visible()
    assert desktop.locator(".operativa-perp").is_visible()
    desktop.locator("#t-range").select_option("1y")
    expect(desktop.locator('[data-chart-range="t-chart-cv"]')).to_contain_text("2025")
    check_chart(desktop, "#/portfolio", "p-chart", 420, "/tmp/orbis-paper-chart-large.png")
    desktop.locator("#p-tf").select_option("7d")
    expect(desktop.locator("#p-market .ticker-sub")).to_contain_text("7 días:")
    expect(desktop.locator('[data-chart-range="p-chart"]')).to_contain_text("2027")
    desktop.goto(BASE + "#/operativa")
    desktop.locator("#t-chart-cv").wait_for(state="visible")
    desktop.set_viewport_size({"width": 1024, "height": 768})
    resized = desktop.locator("#t-chart-cv").bounding_box()
    assert resized and resized["height"] >= 440 and resized["width"] < 1024
    desktop.wait_for_function("""() => {
      const host = document.querySelector('#t-chart-cv');
      const canvas = host && host.querySelector('canvas');
      return host && canvas && canvas.getBoundingClientRect().width <= host.getBoundingClientRect().width;
    }""")
    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    configure(mobile)
    check_chart(mobile, "#/inicio", "home-chart-cv", 280, "/tmp/orbis-inicio-chart-mobile.png")
    check_chart(mobile, "#/operativa", "t-chart-cv", 280, "/tmp/orbis-operativa-chart-mobile.png")
    assert not mobile.evaluate("document.documentElement.scrollWidth > innerWidth")
    fallback = browser.new_page(viewport={"width": 1200, "height": 800})
    configure(fallback, fallback=True)
    check_chart(fallback, "#/operativa", "t-chart-cv", 440, "/tmp/orbis-operativa-chart-fallback.png")
    canvas = fallback.locator("#t-chart-cv")
    assert canvas.evaluate("node => node.height >= node.clientHeight"), "El buffer canvas no debe seguir en 200 px"
    stale = browser.new_page(viewport={"width": 1200, "height": 800})
    configure(stale)
    old_points = [{"t": 1_729_728_000 + day * 86400, "open": "100", "high": "102",
                   "low": "99", "close": str(100 + day)} for day in range(8)]
    stale.route("**/market/history?*", lambda route: route.fulfill(status=200, json={
        "points": old_points, "source": "kraken", "stale": True,
        "note": "La última vela es antigua para esta temporalidad."}))
    stale.goto(BASE + "#/inicio")
    expect(stale.locator("#home-market .ticker-sub")).to_contain_text("24h: —")
    expect(stale.locator("#home-market .amber")).to_contain_text("Histórico desactualizado")
    expect(stale.locator('[data-chart-range="home-chart-cv"]')).to_contain_text("2024")
    stale.goto(BASE + "#/operativa")
    expect(stale.locator("#t-market .ticker-sub")).to_contain_text("24h: —")
    expect(stale.locator("#t-chart .amber")).to_contain_text("Histórico desactualizado")
    stale.goto(BASE + "#/portfolio")
    stale.locator("#p-tf").select_option("7d")
    expect(stale.locator("#p-market .ticker-sub")).to_contain_text("7 días: —")
    expect(stale.locator("#p-market .amber")).to_contain_text("Histórico desactualizado")
    browser.close()
    print("Inicio, Operativa y PAPER: gráficos altos y series antiguas etiquetadas OK")
