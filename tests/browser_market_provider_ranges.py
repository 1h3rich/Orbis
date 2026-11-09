"""La fuente visible gobierna las velas y PAPER descarta respuestas antiguas."""

import os
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright
from browser_paper_preview_mock import mock_preview


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000/ui/")
NOW = int(time.time())


def candles(count, step):
    return [{"t": NOW - (count - i) * step, "o": "100", "h": "102",
             "l": "99", "c": str(100 + i % 10)} for i in range(count)]


requests = []


def mock_candles(route):
    query = parse_qs(urlparse(route.request.url).query)
    requests.append(query)
    short = query.get("timeframe") == ["1h"]
    route.fulfill(status=200, json={
        "available": True, "resolved_provider": query["provider"][0],
        "candles": candles(168 if short else 1000, 3600 if short else 86400),
    })


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.add_init_script("localStorage.setItem('orbis.provider.default', 'mexc')")
    page.add_init_script("""(() => {
      const originalFetch = window.fetch;
      window.fetch = (...args) => originalFetch(...args).then(response => {
        const url = String(args[0]);
        return url.includes('/market/candles?') && url.includes('provider=mexc') &&
          url.includes('timeframe=1d')
          ? new Promise(resolve => setTimeout(() => resolve(response), 800)) : response;
      });
    })();""")
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "123", "source": "kraken", "age_seconds": 1, "stale": False}))
    page.route("**/paper/market/preview?*", mock_preview)
    page.route("**/market/candles?*", mock_candles)
    page.goto(BASE + "#/inicio")
    page.locator("#home-range").select_option("7d")
    expect(page.locator('[data-chart-range="home-chart-cv"]')).to_be_visible()
    assert requests[-1]["provider"] == ["mexc"]
    assert requests[-1]["timeframe"] == ["1h"]
    page.goto(BASE + "#/operativa")
    page.locator("#t-range").select_option("5y")
    expect(page.locator("#t-chart .amber")).to_contain_text("Cobertura limitada")
    assert requests[-1]["provider"] == ["mexc"]
    assert requests[-1]["timeframe"] == ["1d"]
    page.locator("#provider-sel").select_option("kraken")
    expect(page.locator("#t-chart .amber")).to_contain_text("Cobertura limitada")
    assert requests[-1]["provider"] == ["kraken"]
    expect(page.locator("#t-chart details")).to_contain_text("Kraken")
    page.locator("#provider-sel").select_option("mexc")
    page.goto(BASE + "#/portfolio")
    page.locator("#p-tf").select_option("5y")
    page.locator("#p-tf").select_option("7d")
    expect(page.locator("#p-market details")).to_contain_text("periodo 7d")
    page.wait_for_timeout(1000)
    expect(page.locator("#p-market details")).to_contain_text("periodo 7d")
    assert "MEXC" in page.locator("#p-market details").text_content()
    browser.close()
    print("Inicio/Operativa/PAPER: fuente seleccionada y respuestas fuera de orden OK")
