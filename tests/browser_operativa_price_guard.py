"""Operativa PAPER no reutiliza un precio anterior ni uno marcado atrasado.

Todas las peticiones de escritura PAPER se interceptan: no se toca la cartera
local. Las cotizaciones e históricos también se sustituyen por datos fijos.
"""

import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000/ui/")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    quote_mode = {"BTC": "fresh", "ETH": "error"}
    posted = []
    sold = []

    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": False, "candles": [], "reason": "Histórico de prueba vacío",
    }))
    page.route("**/paper/portfolio?*", lambda route: route.fulfill(
        status=404, json={"detail": "Paper scenario not found"}))

    def market_price(route):
        asset = "ETH" if "asset=ETH" in route.request.url else "BTC"
        mode = quote_mode[asset]
        if mode == "error":
            route.fulfill(status=503, json={"detail": "Market unavailable"})
        else:
            route.fulfill(status=200, json={
                "asset": asset, "quote_currency": "EUR", "price": "100" if asset == "BTC" else "20",
                "buy_price": None if mode == "sell_only" else ("101" if asset == "BTC" else "21"),
                "sell_price": "99" if asset == "BTC" else "19",
                "available": mode in ("fresh", "approx", "sell_only"), "provider": "kraken",
                "source": "kraken" if mode in ("fresh", "approx", "sell_only") else "stale-cache",
                "age_seconds": 1 if mode in ("fresh", "approx", "sell_only") else 600,
                "stale": mode == "stale",
                "buy_approximate": mode == "approx", "sell_approximate": mode == "approx",
            })

    def paper_buy(route):
        posted.append(route.request.post_data_json)
        route.fulfill(status=200, json={"executed": True, "price": "100", "quote": {
            "symbol": "BTC/EUR", "provider": "kraken", "approximate": True,
        }})

    def paper_sell(route):
        sold.append(route.request.post_data_json)
        route.fulfill(status=200, json={"executed": True})

    page.route("**/paper/market/preview?*", market_price)
    page.route("**/paper/market/buy", paper_buy)
    page.route("**/paper/market/sell", paper_sell)
    page.goto(BASE + "#/operativa")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#buy-price")).to_contain_text("101")
    expect(page.locator("#sell-price")).to_contain_text("99")
    expect(page.locator("#f-buy .marketprice")).to_contain_text("Precio ref.")
    assert "cotización pública nueva" in page.locator("#f-buy .marketprice").get_attribute("data-help")

    page.locator("#f-buy").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-buy")).to_contain_text("Compra lista")
    expect(page.locator("#r-buy")).to_contain_text("Precio aplicado PAPER")
    assert len(posted) == 1 and posted[0]["asset"] == "BTC" and "price" not in posted[0]

    exact_amount = "123456789012345678.123456789012"
    page.locator('#f-buy input[name="order_amount"]').fill(exact_amount)
    page.locator("#f-buy details.adv summary").click()
    page.locator('#f-buy input[name="estimated_fee"]').fill("0.000000000001")
    expect(page.locator("#buy-total")).to_contain_text("123.456.789.012.345.678,12")
    page.locator('#f-buy input[name="estimated_fee"]').fill("x")
    expect(page.locator("#buy-total")).to_have_text("")
    page.locator('#f-buy input[name="estimated_fee"]').fill("0.000000000001")
    page.locator("#f-buy").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-buy")).to_contain_text("Compra lista")
    assert posted[1]["order_amount"] == exact_amount
    assert posted[1]["estimated_fee"] == "0.000000000001"
    assert isinstance(posted[1]["order_amount"], str) and "price" not in posted[1]

    page.evaluate("""() => {
      const later = performance.now() + 61_000;
      Object.defineProperty(performance, 'now', {configurable: true, value: () => later});
    }""")
    page.locator("#f-buy").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-buy")).to_contain_text("cotización actual de este par")
    expect(page.locator("#t-market")).to_contain_text("cotización ha caducado")
    assert len(posted) == 2, "Una cotización de más de 60 s no puede reutilizarse"
    page.evaluate("delete performance.now")

    page.locator("#t-asset").select_option("ETH")
    expect(page.locator("#buy-price")).to_have_text("—")
    expect(page.locator("#t-market .state.error")).to_be_visible()
    page.locator("#f-buy").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-buy")).to_contain_text("cotización actual de este par")
    assert len(posted) == 2, "El precio BTC no puede enviarse para ETH"

    quote_mode["BTC"] = "stale"
    page.locator("#t-asset").select_option("BTC")
    expect(page.locator("#t-market .badge.stale")).to_be_visible()
    expect(page.locator("#buy-price")).to_have_text("—")
    page.locator("#f-buy").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-buy")).to_contain_text("cotización actual de este par")
    assert len(posted) == 2, "Una cotización atrasada no puede originar la operación"
    page.locator("#tab-sell").click()
    page.locator("#f-sell").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-sell")).to_contain_text("cotización actual de este par")
    assert not sold, "Una venta tampoco puede enviarse con cotización atrasada"

    quote_mode["BTC"] = "approx"
    page.locator("#t-reload").click()
    expect(page.locator("#t-market .badge.approx")).to_contain_text("Precio aprox.")
    expect(page.locator("#buy-price")).to_contain_text("≈")
    expect(page.locator("#sell-price")).to_contain_text("≈")

    quote_mode["BTC"] = "sell_only"
    page.locator("#t-reload").click()
    expect(page.locator("#buy-price")).to_have_text("—")
    expect(page.locator("#sell-price")).to_contain_text("99")
    page.locator("#tab-buy").click()
    page.locator("#f-buy").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-buy")).to_contain_text("cotización actual de este par")
    assert len(posted) == 2
    page.locator("#tab-sell").click()
    page.locator('#f-sell input[name="quantity"]').fill("1234567890123.123456789012")
    expect(page.locator("#sell-total")).to_contain_text("122.222.221.122.189,22")
    page.locator("#f-sell").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-sell")).to_contain_text("Venta")
    assert len(sold) == 1

    browser.close()
