"""Estudio distingue hora del mercado, recepción local y dato atrasado."""

import json
import os
import time

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
BAR = (int(time.time()) // 3600 - 2) * 3600
state = {"status": "unknown"}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 850})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def candles(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "available": True, "symbol": "BTC/EUR", "resolved_provider": "kraken",
            "timeframe": "1h", "candles": [
                {"t": BAR, "o": "100", "h": "102", "l": "99", "c": "101", "v": "2", "closed": True},
                {"t": BAR + 3600, "o": "101", "h": "103", "l": "100", "c": "102", "v": "3", "closed": True},
            ], "older_pagination_supported": False,
        }))

    def snapshot(route):
        status = state["status"]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "available": True, "symbol": "BTC/EUR", "resolved_provider": "kraken",
            "price": "102", "bid": None, "ask": None, "change_pct": None,
            "received_at": "2026-10-22T22:00:00+00:00",
            "market_time_at": None if status == "unknown" else "2026-10-22T21:58:00+00:00",
            "market_age_seconds": None if status == "unknown" else (90 if status == "stale" else 5),
            "market_time_status": status,
        }))

    page.route("**/market/candles?*", candles)
    page.route("**/market/snapshot?*", snapshot)
    page.goto(BASE + "/ui/#/estudio")
    page.wait_for_load_state("networkidle")
    source = page.locator("#st-price-source")
    expect(source).to_contain_text("hora del mercado no publicada")
    expect(source).to_have_attribute("data-state", "unknown")
    expect(source).to_have_attribute("title", "Consulta local: 2026-10-22T22:00:00+00:00. Hora del mercado: no publicada. Consultar no confirma por sí solo que el precio sea ejecutable.")

    state["status"] = "stale"
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(source).to_contain_text("dato atrasado · 1 min")
    expect(source).to_have_attribute("data-state", "stale")
    assert source.evaluate("el => getComputedStyle(el).color") != "rgb(0, 0, 0)"

    state["status"] = "fresh"
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(source).to_contain_text("mercado hace 5 s")
    expect(source).to_have_attribute("data-state", "fresh")
    assert not errors, errors
    browser.close()
