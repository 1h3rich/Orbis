"""Una respuesta tardía no debe cambiar la divisa visible de Cartera PAPER."""

import json
import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000")


def portfolio(quote, equity):
    return {
        "quote_currency": quote, "cash_balance": equity, "cash_weight": "100",
        "positions_value": "0", "equity": equity, "total_pnl": "0",
        "return_percentage": "0", "valuation_status": "CASH_ONLY", "positions": [],
    }


def portfolio_with_btc(quote, equity):
    result = portfolio(quote, equity)
    result.update({
        "cash_balance": str(int(equity) - 100),
        "cash_weight": "90", "positions_value": "100",
        "valuation_status": "ESTIMATED",
        "positions": [{
            "asset": "BTC", "quote_currency": quote, "quantity": "0.001",
            "average_cost": "100000", "invested": "100",
            "current_price": "100000", "value": "100",
            "unrealized_pnl": "0", "realized_pnl": "0",
            "weight_percentage": "10", "price_source": "last_trade",
            "price_as_of": None, "price_age_seconds": None,
            "price_stale": True,
        }],
    })
    return result


def wait_pending(page, bucket, quote, expected):
    for _ in range(40):
        count = page.evaluate(
            "([bucket, quote]) => window[bucket][quote].length", [bucket, quote]
        )
        if count == expected:
            return
        page.wait_for_timeout(25)
    raise AssertionError(f"{bucket}.{quote}: se esperaban {expected}, hay {count}")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
      const realFetch = window.fetch.bind(window);
      window.__portfolioPending = { EUR: [], USD: [] };
      window.__operationsPending = { EUR: [], USD: [] };
      window.__finishPortfolio = function(quote, payload, status = 200, index = 0) {
        const resolve = window.__portfolioPending[quote].splice(index, 1)[0];
        if (!resolve) throw new Error("Sin petición PAPER pendiente para " + quote);
        resolve(new Response(JSON.stringify(payload), {
          status, headers: { "Content-Type": "application/json" }
        }));
      };
      window.__finishOperations = function(quote, count) {
        const resolve = window.__operationsPending[quote].shift();
        if (!resolve) throw new Error("Sin operaciones pendientes para " + quote);
        resolve(new Response(JSON.stringify(Array(count).fill({})), {
          status: 200, headers: { "Content-Type": "application/json" }
        }));
      };
      window.fetch = function(input, options) {
        const raw = typeof input === "string" ? input : input.url;
        const url = new URL(raw, location.href);
        if (url.pathname === "/paper/portfolio") {
          const quote = url.searchParams.get("quote_currency");
          return new Promise(resolve => window.__portfolioPending[quote].push(resolve));
        }
        if (url.pathname === "/paper/operations" && url.searchParams.has("asset")) {
          const quote = url.searchParams.get("quote");
          return new Promise(resolve => window.__operationsPending[quote].push(resolve));
        }
        return realFetch(input, options);
      };
    })();""")
    page.route("**/paper/operations?*", lambda route: route.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/market/**", lambda route: route.fulfill(
        status=503, content_type="application/json", body="{}"))

    page.goto(BASE + "/ui/#/portfolio")
    page.wait_for_load_state("networkidle")
    wait_pending(page, "__portfolioPending", "EUR", 1)
    page.locator("#p-quote").select_option("USD")
    wait_pending(page, "__portfolioPending", "USD", 1)
    page.evaluate("window.__finishPortfolio('USD', %s)" % json.dumps(portfolio("USD", "2000")))
    expect(page.locator("#p-hero")).to_contain_text("2000")
    page.evaluate("window.__finishPortfolio('EUR', %s)" % json.dumps(portfolio("EUR", "1000")))
    page.wait_for_timeout(50)
    assert page.locator("#p-quote").input_value() == "USD"
    assert "2000" in page.locator("#p-hero").inner_text()
    assert "1000" not in page.locator("#p-hero").inner_text()
    assert "USD" in page.locator("#p-body").inner_text()
    assert "Última actualización:" in page.locator("#pf-last-update").inner_text()

    # Dos actualizaciones del mismo saldo: manda la última petición, no la
    # respuesta que llega en último lugar.
    page.locator("#pf-refresh-now").click()
    page.locator("#pf-refresh-now").click()
    wait_pending(page, "__portfolioPending", "USD", 2)
    assert page.locator("#pf-last-update").inner_text() == "Consultando…"
    page.evaluate("window.__finishPortfolio('USD', %s, 200, 1)" % json.dumps(portfolio("USD", "3000")))
    expect(page.locator("#p-hero")).to_contain_text("3000")
    page.evaluate("window.__finishPortfolio('USD', %s)" % json.dumps(portfolio("USD", "2500")))
    page.wait_for_timeout(50)
    assert "3000" in page.locator("#p-hero").inner_text()
    assert "2500" not in page.locator("#p-hero").inner_text()
    assert "Última actualización:" in page.locator("#pf-last-update").inner_text()

    page.locator("#pf-refresh-now").click()
    wait_pending(page, "__portfolioPending", "USD", 1)
    page.evaluate("window.__finishPortfolio('USD', {}, 503)")
    expect(page.locator("#pf-last-update")).to_have_text("No actualizado")

    # La tabla secundaria tampoco puede recibir el recuento de la divisa vieja.
    page.locator("#p-quote").select_option("EUR")
    wait_pending(page, "__portfolioPending", "EUR", 1)
    page.evaluate("window.__finishPortfolio('EUR', %s)" % json.dumps(portfolio_with_btc("EUR", "1000")))
    wait_pending(page, "__operationsPending", "EUR", 1)
    page.locator("#p-quote").select_option("USD")
    wait_pending(page, "__portfolioPending", "USD", 1)
    page.evaluate("window.__finishPortfolio('USD', %s)" % json.dumps(portfolio_with_btc("USD", "2000")))
    wait_pending(page, "__operationsPending", "USD", 1)
    page.evaluate("window.__finishOperations('USD', 3)")
    expect(page.locator('[data-dcacount="BTC"]')).to_have_text("3")
    page.evaluate("window.__finishOperations('EUR', 9)")
    page.wait_for_timeout(50)
    assert page.locator('[data-dcacount="BTC"]').inner_text() == "3"
    assert "USD" in page.locator("#p-body").inner_text()
    assert not errors, errors
    browser.close()
    print("Cartera PAPER: respuesta antigua no cambia la divisa visible")
