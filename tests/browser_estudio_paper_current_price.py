"""El cursor histórico de Estudio no determina el precio de una operación PAPER.

Todas las escrituras se interceptan; esta prueba no modifica la cartera local.
"""

from datetime import datetime, timedelta, timezone
import os
import time

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000/ui/")
BAR = (int(time.time()) // 3600 - 2) * 3600


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 850})
    quote = {"status": "unknown", "old": False}
    reply = {"status": 200}
    posted = []

    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": True, "symbol": "BTC/EUR", "resolved_provider": "kraken", "timeframe": "1h",
        "candles": [
            {"t": BAR, "o": "48", "h": "51", "l": "47", "c": "49", "v": "2", "closed": True},
            {"t": BAR + 3600, "o": "49", "h": "51", "l": "48", "c": "50", "v": "3", "closed": True},
        ], "older_pagination_supported": False,
    }))

    def snapshot(route):
        received = datetime.now(timezone.utc) - (timedelta(seconds=61) if quote["old"] else timedelta(0))
        route.fulfill(status=200, json={
            "available": True, "symbol": "BTC/EUR", "resolved_provider": "kraken", "price": "100",
            "bid": None, "ask": None, "change_pct": None,
            "received_at": received.isoformat(), "market_time_status": quote["status"],
            "market_time_at": None, "market_age_seconds": None,
        })

    def paper_buy(route):
        posted.append(route.request.post_data_json)
        if reply["status"] == 409:
            route.fulfill(status=409, json={"detail": "Conflicto de request_id"})
        elif reply["status"] == 422:
            route.fulfill(status=422, json={"detail": "Importe inválido"})
        elif reply["status"] == 503:
            route.fulfill(status=503, json={"code": "market_quote_unavailable",
                                             "detail": "Cotización pública no disponible"})
        else:
            route.fulfill(status=200, json={"executed": True, "price": "100", "quote": {
                "symbol": "BTC/EUR", "provider": "kraken", "approximate": True,
            }})

    page.route("**/market/snapshot?*", snapshot)
    page.route("**/paper/market/buy", paper_buy)
    page.route("**/paper/market/sell", lambda route: route.fulfill(status=200, json={
        "executed": False, "reason": "Estimated fee must be lower than order amount",
    }))
    page.route("**/paper/operations?*", lambda route: route.fulfill(status=200, json=[]))
    page.route("**/paper/portfolio?*", lambda route: route.fulfill(status=404, json={"detail": "No scenario"}))

    page.goto(BASE + "#/estudio")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#st-price-source")).to_contain_text("hora del mercado no publicada")
    page.locator("#st-buy-open").click()
    expect(page.locator("#st-trade-price")).to_have_value("100")
    expect(page.locator("#st-trade-price")).to_have_attribute("readonly", "")
    page.locator("#st-trade-price").evaluate("el => el.value = '1'")
    page.locator("#st-trade-form").evaluate("form => form.requestSubmit()")
    expect(page.locator("#st-trade-msg")).to_contain_text("Compra lista")
    assert len(posted) == 1 and "price" not in posted[0], posted

    reply["status"] = 409
    page.locator("#st-trade-form").evaluate("form => form.requestSubmit()")
    expect(page.locator("#st-trade-msg")).to_contain_text("Revisa tu historial")
    assert page.locator("#st-trade-msg [data-retry]").count() == 0
    assert len(posted) == 2

    reply["status"] = 422
    page.locator("#st-trade-form").evaluate("form => form.requestSubmit()")
    expect(page.locator("#st-trade-msg")).to_contain_text("Revisa el importe")
    assert page.locator("#st-trade-msg [data-retry]").count() == 0
    assert len(posted) == 3

    reply["status"] = 503
    page.locator("#st-trade-form").evaluate("form => form.requestSubmit()")
    expect(page.locator("#st-trade-msg")).to_contain_text("Reintenta con los mismos datos")
    assert page.locator("#st-trade-msg [data-retry]").count() == 1
    page.locator("#st-trade-msg [data-retry]").click()
    assert len(posted) == 5 and posted[3]["request_id"] == posted[4]["request_id"]

    page.locator("#st-sell-open").click()
    page.locator("#st-trade-form").evaluate("form => form.requestSubmit()")
    expect(page.locator("#st-trade-msg")).to_contain_text("0,10")
    expect(page.locator("#st-trade-msg")).to_contain_text("menor que el importe")
    assert len(posted) == 5

    quote["status"] = "stale"
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#st-buy-open").click()
    expect(page.locator("#st-trade-price")).to_have_value("")
    page.locator("#st-trade-form").evaluate("form => form.requestSubmit()")
    expect(page.locator("#st-trade-msg")).to_contain_text("Sin cotización actual")
    assert len(posted) == 5

    quote["status"] = "unknown"
    quote["old"] = True
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#st-buy-open").click()
    expect(page.locator("#st-trade-price")).to_have_value("")
    page.locator("#st-trade-form").evaluate("form => form.requestSubmit()")
    expect(page.locator("#st-trade-msg")).to_contain_text("Sin cotización actual")
    assert len(posted) == 5
    browser.close()
