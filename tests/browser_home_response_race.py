"""Inicio conserva el resumen más reciente ante respuestas HTTP tardías."""

import json
import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000")


def portfolio(value):
    return {
        "quote_currency": "EUR", "cash_balance": str(value),
        "positions_value": "0", "equity": str(value), "total_pnl": "0",
        "return_percentage": "0", "valuation_status": "CASH_ONLY",
        "positions": [],
    }


def operation(asset):
    return [{
        "operation_type": "BUY", "asset": asset, "quote_currency": "EUR",
        "amount_spent": "100", "timestamp": "2026-10-21T10:00:00+00:00",
    }]


def wait_pending(page, bucket, count):
    for _ in range(80):
        actual = page.evaluate("bucket => window.__homePending[bucket].length", bucket)
        if actual == count:
            return
        page.wait_for_timeout(25)
    raise AssertionError(f"{bucket}: se esperaban {count}, hay {actual}")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
      const realFetch = window.fetch.bind(window);
      const realSetInterval = window.setInterval.bind(window);
      window.setInterval = function(fn, ms, ...rest) {
        if (ms === 60000) window.__homeTick = fn;
        return realSetInterval(fn, ms, ...rest);
      };
      window.__homePending = { portfolio: [], operations: [], watchlist: [], strategies: [] };
      window.__finishHome = function(bucket, payload, index = 0) {
        const resolve = window.__homePending[bucket].splice(index, 1)[0];
        if (!resolve) throw new Error("Sin petición pendiente: " + bucket);
        resolve(new Response(JSON.stringify(payload), {
          status: 200, headers: { "Content-Type": "application/json" }
        }));
      };
      window.fetch = function(input, options) {
        const raw = typeof input === "string" ? input : input.url;
        const url = new URL(raw, location.href);
        let bucket = null;
        if (url.pathname === "/paper/portfolio") bucket = "portfolio";
        else if (url.pathname === "/paper/operations" && url.searchParams.has("limit")) bucket = "operations";
        else if (url.pathname === "/ux/watchlist") bucket = "watchlist";
        else if (url.pathname === "/strategies") bucket = "strategies";
        if (bucket) return new Promise(resolve => window.__homePending[bucket].push(resolve));
        return realFetch(input, options);
      };
    })();""")
    page.route("**/market/**", lambda route: route.fulfill(
        status=503, content_type="application/json", body="{}"))

    page.goto(BASE + "/ui/#/inicio")
    page.wait_for_load_state("networkidle")
    for bucket in ("portfolio", "operations", "watchlist", "strategies"):
        wait_pending(page, bucket, 1)

    # Forzar el tick del refresco sin esperar un minuto de reloj real.
    page.evaluate("window.__homeTick()")
    for bucket in ("portfolio", "operations", "watchlist", "strategies"):
        wait_pending(page, bucket, 2)

    new = {
        "portfolio": portfolio(2000), "operations": operation("ETH"),
        "watchlist": ["ETH/EUR"],
        "strategies": [{"name": "Nueva", "enabled": False,
                        "config": {"asset": "ETH", "quote_currency": "EUR"}}],
    }
    old = {
        "portfolio": portfolio(1000), "operations": operation("BTC"),
        "watchlist": ["BTC/EUR"],
        "strategies": [{"name": "Antigua", "enabled": False,
                        "config": {"asset": "BTC", "quote_currency": "EUR"}}],
    }
    for bucket, payload in new.items():
        page.evaluate("([bucket, payload]) => window.__finishHome(bucket, payload, 1)",
                      [bucket, payload])
    expect(page.locator("#home-stats")).to_contain_text("2.000,00")
    expect(page.locator("#home-ops")).to_contain_text("ETH/EUR")
    expect(page.locator("#home-wl")).to_contain_text("ETH/EUR")
    expect(page.locator("#home-strat")).to_contain_text("Nueva")

    for bucket, payload in old.items():
        page.evaluate("([bucket, payload]) => window.__finishHome(bucket, payload)",
                      [bucket, payload])
    page.wait_for_timeout(50)
    assert "2.000,00" in page.locator("#home-stats").inner_text()
    assert "1.000,00" not in page.locator("#home-stats").inner_text()
    assert "ETH/EUR" in page.locator("#home-ops").inner_text()
    assert "BTC/EUR" not in page.locator("#home-ops").inner_text()
    assert "ETH/EUR" in page.locator("#home-wl").inner_text()
    assert "BTC/EUR" not in page.locator("#home-wl").inner_text()
    assert "Nueva" in page.locator("#home-strat").inner_text()
    assert "Antigua" not in page.locator("#home-strat").inner_text()

    # Si la petición antigua termina primero, aún puede mostrar datos hasta
    # que la más reciente responda: una conexión lenta no deja Inicio vacío.
    page.evaluate("window.__homeTick()")
    page.evaluate("window.__homeTick()")
    for bucket in ("portfolio", "operations", "watchlist", "strategies"):
        wait_pending(page, bucket, 2)
    intermediate = {
        "portfolio": portfolio(3000), "operations": operation("SOL"),
        "watchlist": ["SOL/EUR"],
        "strategies": [{"name": "Intermedia", "enabled": False,
                        "config": {"asset": "SOL", "quote_currency": "EUR"}}],
    }
    final = {
        "portfolio": portfolio(4000), "operations": operation("ADA"),
        "watchlist": ["ADA/EUR"],
        "strategies": [{"name": "Final", "enabled": False,
                        "config": {"asset": "ADA", "quote_currency": "EUR"}}],
    }
    for bucket, payload in intermediate.items():
        page.evaluate("([bucket, payload]) => window.__finishHome(bucket, payload)",
                      [bucket, payload])
    expect(page.locator("#home-stats")).to_contain_text("3.000,00")
    expect(page.locator("#home-ops")).to_contain_text("SOL/EUR")
    for bucket, payload in final.items():
        page.evaluate("([bucket, payload]) => window.__finishHome(bucket, payload)",
                      [bucket, payload])
    expect(page.locator("#home-stats")).to_contain_text("4.000,00")
    expect(page.locator("#home-ops")).to_contain_text("ADA/EUR")
    expect(page.locator("#home-wl")).to_contain_text("ADA/EUR")
    expect(page.locator("#home-strat")).to_contain_text("Final")

    # Una generación de Inicio abandonada no toca la vista al volver a Inicio.
    page.evaluate("window.__homeTick()")
    for bucket in ("portfolio", "operations", "watchlist", "strategies"):
        wait_pending(page, bucket, 1)
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.goto(BASE + "/ui/#/inicio")
    page.wait_for_load_state("networkidle")
    for bucket in ("portfolio", "operations", "watchlist", "strategies"):
        wait_pending(page, bucket, 2)
    returned = {
        "portfolio": portfolio(5000), "operations": operation("DOT"),
        "watchlist": ["DOT/EUR"],
        "strategies": [{"name": "Reabierta", "enabled": False,
                        "config": {"asset": "DOT", "quote_currency": "EUR"}}],
    }
    abandoned = {
        "portfolio": portfolio(9000), "operations": operation("AVAX"),
        "watchlist": ["AVAX/EUR"],
        "strategies": [{"name": "Abandonada", "enabled": False,
                        "config": {"asset": "AVAX", "quote_currency": "EUR"}}],
    }
    for bucket, payload in returned.items():
        page.evaluate("([bucket, payload]) => window.__finishHome(bucket, payload, 1)",
                      [bucket, payload])
    expect(page.locator("#home-stats")).to_contain_text("5.000,00")
    for bucket, payload in abandoned.items():
        page.evaluate("([bucket, payload]) => window.__finishHome(bucket, payload)",
                      [bucket, payload])
    page.wait_for_timeout(50)
    assert "5.000,00" in page.locator("#home-stats").inner_text()
    assert "9.000,00" not in page.locator("#home-stats").inner_text()
    assert "DOT/EUR" in page.locator("#home-ops").inner_text()
    assert "AVAX/EUR" not in page.locator("#home-ops").inner_text()
    page.locator("#home-quote").select_option("USDT")
    wait_pending(page, "portfolio", 1)
    page.locator("#home-quote").select_option("EUR")
    wait_pending(page, "portfolio", 2)
    page.evaluate("payload => window.__finishHome('portfolio', payload, 1)", portfolio(4000))
    expect(page.locator("#home-stats")).to_contain_text("4.000,00")
    page.evaluate("payload => window.__finishHome('portfolio', payload)",
                  {**portfolio(8000), "quote_currency": "USDT"})
    page.wait_for_timeout(50)
    assert "4.000,00" in page.locator("#home-stats").inner_text()
    assert "8.000,00" not in page.locator("#home-stats").inner_text()
    expect(page.locator("#home-quote")).to_have_value("EUR")
    assert not errors, errors
    browser.close()
    print("Inicio: respuestas antiguas no sustituyen el resumen más reciente")
