"""Un fallo de respuesta no crea otro request_id al reintentar PAPER.

Las escrituras se interceptan; no modifica saldos del usuario.
"""

from playwright.sync_api import expect, sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    posts = []
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "price": "100", "source": "kraken", "age_seconds": 1, "stale": False,
    }))
    page.route("**/paper/market/preview?*", lambda route: route.fulfill(status=200, json={
        "available": True, "symbol": "BTC/EUR", "provider": "kraken",
        "price": "100", "buy_price": "100", "sell_price": "100",
        "source": "kraken", "age_seconds": 1, "stale": False,
    }))
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": False, "candles": [], "reason": "Sin histórico de prueba",
    }))
    page.route("**/paper/portfolio?*", lambda route: route.fulfill(
        status=404, json={"detail": "No scenario"}))

    def paper_buy(route):
        posts.append(route.request.post_data_json)
        if len(posts) == 1:
            route.abort("failed")
        else:
            route.fulfill(status=200, json={
                "executed": True, "price": "100", "quote": {
                    "symbol": "BTC/EUR", "provider": "kraken", "approximate": True,
                },
            })

    page.route("**/paper/market/buy", paper_buy)
    page.goto("http://127.0.0.1:8000/ui/#/operativa")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#buy-price")).to_contain_text("100")
    page.locator("#f-buy").evaluate("form => form.requestSubmit()")
    expect(page.locator("#r-buy")).to_contain_text("No hemos podido conectar")
    page.locator("#r-buy [data-retry]").click()
    expect(page.locator("#r-buy")).to_contain_text("Compra lista")
    assert len(posts) == 2
    assert posts[0] == posts[1]
    assert "price" not in posts[0]
    browser.close()
