"""Estudio: rango de fechas de la gráfica genérica, sin datos externos."""

from datetime import datetime, timedelta, timezone
import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8765") + "/ui/#/estudio"
START = datetime(2012, 1, 1, tzinfo=timezone.utc)
ROWS = []
for day in range(5400):
    stamp = int((START + timedelta(days=day)).timestamp())
    ROWS.append({"t": stamp, "o": "100", "h": "101", "l": "99", "c": "100", "v": "10", "closed": True})


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/market/btc-daily-archive?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "bitstamp", "timeframe": "1d",
        "first_candle_at": ROWS[0]["t"], "last_candle_at": ROWS[-1]["t"],
        "missing_days": 0, "candles": ROWS,
    }))
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "bitstamp", "timeframe": "1d",
        "older_pagination_supported": False, "candles": ROWS[-300:],
    }))
    page.route("**/market/snapshot?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "bitstamp", "price": "100",
        "timestamp": ROWS[-1]["t"],
    }))
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    page.locator("#st-pair").fill("BTC/USD")
    page.locator("#st-prov").select_option("bitstamp")
    page.locator("#st-tf").select_option("1d")
    page.locator("#st-range").select_option("custom")
    expect(page.locator("#st-range-dates")).to_be_visible()
    assert page.locator("#st-chart").get_attribute("data-archive-window") != "custom"
    page.screenshot(path="/tmp/orbis-estudio-selector-fechas.png", full_page=False)
    page.locator("#st-range-from").fill("2018-01-01")
    page.locator("#st-range-to").fill("2017-12-31")
    page.locator("#st-range-apply").click()
    expect(page.locator("#st-range-error")).to_be_visible()
    assert page.locator("#st-chart").get_attribute("data-archive-window") != "custom"
    page.locator("#st-range-to").fill("2020-12-31")
    page.locator("#st-range-apply").click()
    expect(page.locator("#st-range-dates")).to_be_hidden()
    expected_from = int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp())
    expected_to = int(datetime(2020, 12, 31, tzinfo=timezone.utc).timestamp())
    page.wait_for_function("""([start, end]) => {
      const chart = document.querySelector('#st-chart');
      return chart && chart.dataset.archiveFrom === String(start) &&
        chart.dataset.archiveTo === String(end);
    }""", arg=[expected_from, expected_to], timeout=30000)
    assert "Archivo 2012-01-01" in page.locator("#st-archive-meta").inner_text()
    page.screenshot(path="/tmp/orbis-estudio-fechas-personalizadas.png", full_page=True)
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("#st-range").input_value() == "custom"
    assert page.locator("#st-range-from").input_value() == "2018-01-01"
    assert page.locator("#st-range-to").input_value() == "2020-12-31"
    expect(page.locator("#st-chart")).to_have_attribute("data-archive-window", "custom")
    page.locator("#st-range-edit").click()
    page.locator("#st-range-from").fill("2014-01-01")
    page.locator("#st-range-cancel").click()
    assert page.locator("#st-range-from").input_value() == "2018-01-01"
    expect(page.locator("#st-chart")).to_have_attribute("data-archive-window", "custom")
    page.locator(".st-view-actions summary").click()
    expect(page.locator("#st-archive-year")).to_be_visible()
    page.locator("#st-archive-year").select_option("2012")
    expect(page.locator("#st-chart")).to_have_attribute("data-archive-window", "year:2012")
    page.locator("#st-range").select_option("current")
    expect(page.locator("#st-range-dates")).to_be_hidden()
    page.locator("#st-prov").select_option("auto")
    page.locator("#st-range").select_option("custom")
    expect(page.locator("#st-range-dates")).to_be_visible()
    page.locator("#st-tf").select_option("1h")
    expect(page.locator("#st-range-dates")).to_be_hidden()
    assert page.locator("#st-range").input_value() == "current"
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#st-range").select_option("custom")
    expect(page.locator("#st-range-dates")).to_be_visible()
    box = page.locator("#st-range-dates").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    assert not errors, errors
    browser.close()
    print("Estudio fechas personalizadas: archivo, rango y recarga OK")

