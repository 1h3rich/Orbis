"""Inicio y Operativa usan fechas elegidas en la gráfica, con caché separada."""

from datetime import datetime, timedelta, timezone
import os

from playwright.sync_api import expect, sync_playwright
from browser_paper_preview_mock import mock_preview


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8765/ui/")
START = datetime(2012, 1, 1, tzinfo=timezone.utc)
DAILY = []
for day in range(5400):
    stamp = int((START + timedelta(days=day)).timestamp())
    DAILY.append({"t": stamp, "o": "100", "h": "102", "l": "99", "c": str(100 + day / 100)})
HOURLY = [{"t": DAILY[-1]["t"] + hour * 3600, "open": "100", "high": "102",
           "low": "99", "close": "101"} for hour in range(24)]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "101", "source": "bitstamp", "age_seconds": 0, "stale": False,
    }))
    page.route("**/paper/market/preview?*", mock_preview)
    page.route("**/market/history?*", lambda route: route.fulfill(status=200, json={
        "points": HOURLY, "source": "bitstamp", "timeframe": "24h",
    }))
    archive_calls = []
    def archive(route):
        archive_calls.append(route.request.url)
        route.fulfill(status=200, json={
            "available": True, "resolved_provider": "bitstamp", "timeframe": "1d",
            "candles": DAILY,
        })
    page.route("**/market/btc-daily-archive?*", archive)
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "bitstamp", "timeframe": "1d",
        "candles": DAILY[-1000:], "older_pagination_supported": False,
    }))
    page.goto(BASE + "#/inicio")
    page.wait_for_load_state("networkidle")
    before = len(archive_calls)
    page.locator("#home-range").select_option("custom")
    expect(page.locator("#home-range-dates")).to_be_visible()
    assert page.locator("#home-range").input_value() == "24h"
    page.locator("#home-range-from").fill("2018-01-01")
    page.locator("#home-range-to").fill("2020-12-31")
    assert len(archive_calls) == before
    page.locator("#home-range-apply").click()
    expect(page.locator('[data-chart-range="home-chart-cv"]')).to_contain_text("2020", timeout=30000)
    home = page.locator('[data-chart-range="home-chart-cv"]').inner_text()
    assert "2018" in home and "2020" in home, home
    page.screenshot(path="/tmp/orbis-inicio-fechas-personalizadas.png", full_page=True)
    page.locator("#home-range-edit").click()
    page.locator("#home-range-to").fill("2021-12-31")
    assert page.locator('[data-chart-range="home-chart-cv"]').inner_text() == home
    page.locator("#home-range-apply").click()
    expect(page.locator('[data-chart-range="home-chart-cv"]')).to_contain_text("2021", timeout=30000)
    page.goto(BASE + "#/operativa")
    page.wait_for_load_state("networkidle")
    page.locator("#t-range").select_option("custom")
    expect(page.locator("#t-range-dates")).to_be_visible()
    page.locator("#t-range-from").fill("2018-01-01")
    page.locator("#t-range-to").fill("2020-12-31")
    page.locator("#t-range-apply").click()
    expect(page.locator('[data-chart-range="t-chart-cv"]')).to_contain_text("2020", timeout=30000)
    op = page.locator('[data-chart-range="t-chart-cv"]').inner_text()
    assert "2018" in op and "2020" in op, op
    page.locator("#t-range-edit").click()
    page.locator("#t-range-from").fill("2022-01-01")
    page.locator("#t-range-apply").click()
    expect(page.locator("#t-range-error")).to_contain_text("en orden")
    assert page.locator('[data-chart-range="t-chart-cv"]').inner_text() == op
    page.locator("#t-range-cancel").click()
    assert page.locator("#t-range-from").input_value() == "2018-01-01"
    page.screenshot(path="/tmp/orbis-operativa-fechas-personalizadas.png", full_page=True)
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("#t-range").input_value() == "custom"
    assert page.locator("#t-range-from").input_value() == "2018-01-01"
    assert page.locator("#t-range-to").input_value() == "2020-12-31"
    expect(page.locator("#t-range-dates")).to_be_hidden()
    page.locator("#t-range").select_option("24h")
    expect(page.locator("#t-range-dates")).to_be_hidden()
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#t-range").select_option("custom")
    popup = page.locator("#t-range-dates").bounding_box()
    assert popup["x"] >= 0 and popup["x"] + popup["width"] <= 390
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    assert not errors, errors
    browser.close()
    print("Inicio/Operativa fechas: intervalo, caché y recarga OK")

