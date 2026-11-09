"""Estudio conoce intervalos nativos antes de solicitar velas imposibles."""

import json
import os
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
DAY = (int(time.time()) // 86400 - 3) * 86400


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    requests = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_candles(route):
        query = parse_qs(urlparse(route.request.url).query)
        provider = query.get("provider", ["auto"])[0]
        tf = query.get("timeframe", ["1h"])[0]
        requests.append((provider, tf))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "available": True, "resolved_provider": provider if provider != "auto" else "kraken",
            "symbol": "BTC/EUR", "timeframe": tf,
            "candles": [{"t": DAY, "o": "100", "h": "102", "l": "99",
                         "c": "101", "v": "2", "closed": True}],
            "older_pagination_supported": provider == "coinbase",
        }))

    page.route("**/market/candles?*", serve_candles)
    page.goto(BASE + "/ui/#/estudio")
    page.wait_for_load_state("networkidle")
    page.locator("#st-chart").wait_for(state="visible")

    page.locator("#st-prov").select_option("coinbase")
    assert page.locator('#st-tf option[value="30m"]').get_attribute("disabled") is not None
    assert page.locator('#st-tf option[value="4h"]').get_attribute("disabled") is not None
    assert page.locator('#st-tf option[value="15m"]').get_attribute("disabled") is None
    page.locator("#st-tf").select_option("15m")
    expect(page.locator("#st-tf")).to_have_value("15m")

    page.locator("#st-prov").select_option("bitstamp")
    expect(page.locator("#st-tf")).to_have_value("1d")
    assert page.locator('#st-tf option[value="1m"]').get_attribute("disabled") is not None
    assert page.locator('#st-tf option[value="1d"]').get_attribute("disabled") is None
    expect(page.locator("#st-tf-note")).to_contain_text("intervalos no disponibles con BITSTAMP")
    page.wait_for_timeout(150)
    assert ("bitstamp", "1d") in requests, requests
    assert ("bitstamp", "15m") not in requests, requests
    assert not errors, errors
    browser.close()
