"""USDT PAPER aislado: saldo, compra, venta y cartera sin mezclar EUR."""

from decimal import Decimal

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8776"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/market/price?*", lambda route: route.fulfill(status=200, json={
        "asset": "BTC", "quote_currency": "USDT", "price": "100000",
        "source": "kraken", "age_seconds": 1, "stale": False,
    }))
    page.route("**/paper/market/preview?*", lambda route: route.fulfill(status=200, json={
        "available": True, "symbol": "BTC/USDT", "provider": "kraken",
        "price": "100000", "buy_price": "100000", "sell_price": "100000",
        "source": "kraken", "age_seconds": 1, "stale": False,
    }))
    page.goto(BASE + "/ui/#/operativa")
    page.wait_for_load_state("networkidle")
    page.locator("details.start summary").click()
    page.locator("#f-scenario [name='quote_currency']").select_option("USDT")
    page.locator("#f-scenario [name='initial_balance']").fill("1000")
    page.locator("#f-scenario button[type='submit']").click()
    expect(page.locator("#r-scenario")).to_contain_text("Saldo creado")
    page.locator("#t-quote").select_option("USDT")
    expect(page.locator("#buy-price")).to_contain_text("100.000,00 USDT")
    page.locator("#f-buy [name='order_amount']").fill("100")
    page.locator("#b-buy").click()
    expect(page.locator("#r-buy")).to_contain_text("Compra lista.")
    expect(page.locator("#r-buy")).to_contain_text("aproximado")
    after_buy = page.request.get(BASE + "/paper/portfolio?quote_currency=USDT")
    assert after_buy.status == 200
    buy_data = after_buy.json()
    assert Decimal(str(buy_data["cash_balance"])) == Decimal("900")
    assert Decimal(str(buy_data["positions"][0]["quantity"])) == Decimal("0.001")

    page.locator("#tab-sell").click()
    page.locator("#f-sell [name='quantity']").fill("0.0004")
    page.locator("#b-sell").click()
    expect(page.locator("#r-sell")).to_contain_text("Venta lista.")
    after_sell = page.request.get(BASE + "/paper/portfolio?quote_currency=USDT")
    assert after_sell.status == 200
    sell_data = after_sell.json()
    assert Decimal(str(sell_data["cash_balance"])) == Decimal("940")
    assert Decimal(str(sell_data["positions"][0]["quantity"])) == Decimal("0.0006")

    page.goto(BASE + "/ui/#/portfolio")
    page.wait_for_load_state("networkidle")
    page.locator("#p-quote").select_option("USDT")
    expect(page.locator("#p-hero .pf-value")).to_contain_text("USDT")
    expect(page.locator("#p-body")).to_contain_text("0,0006 BTC")
    assert "€" not in page.locator("#p-hero").inner_text()
    assert not errors, errors
    browser.close()
