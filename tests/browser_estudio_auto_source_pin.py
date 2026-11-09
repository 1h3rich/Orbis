"""AUTO fija la fuente de velas, ticker y stream del Estudio por intervalo.

Todas las respuestas de mercado son interceptadas; no se consulta ningún
exchange ni se envían operaciones PAPER/REAL.
"""

import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8000/ui/")
NOW = datetime.now(timezone.utc)
THIS_MONTH = int(datetime(NOW.year, NOW.month, 1, tzinfo=timezone.utc).timestamp())
PREVIOUS_MONTH = int(datetime(NOW.year if NOW.month > 1 else NOW.year - 1,
                              NOW.month - 1 if NOW.month > 1 else 12, 1,
                              tzinfo=timezone.utc).timestamp())
NEXT_MONTH = int(datetime(NOW.year + (NOW.month == 12), NOW.month % 12 + 1, 1,
                          tzinfo=timezone.utc).timestamp())


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1360, "height": 850})
    errors = []
    calls = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""window.__orbisWsUrls = []; window.__orbisWs = [];
        window.WebSocket = class {
          constructor(url) {
            window.__orbisWsUrls.push(url);
            window.__orbisWs.push(this);
            setTimeout(() => { if (this.onopen) this.onopen(); }, 0);
          }
          send() {}
          close() { if (this.onclose) this.onclose(); }
          emit(data) { if (this.onmessage) this.onmessage({data: JSON.stringify(data)}); }
        };""")

    def candles(route):
        query = parse_qs(urlparse(route.request.url).query)
        requested = query.get("provider", ["auto"])[0]
        timeframe = query.get("timeframe", ["1h"])[0]
        resolved = "binance" if timeframe == "1M" else "kraken"
        if requested != "auto":
            resolved = requested
        calls.append(("candles", requested, timeframe))
        route.fulfill(status=200, json={
            "available": True, "symbol": "BTC/EUR", "resolved_provider": resolved,
            "timeframe": timeframe, "older_pagination_supported": False,
            "candles": [
                {"t": PREVIOUS_MONTH, "o": "90", "h": "105", "l": "88", "c": "100", "v": "2", "closed": True},
                {"t": THIS_MONTH, "o": "100", "h": "110", "l": "99", "c": "105", "v": "3", "closed": False},
            ],
        })

    def snapshot(route):
        query = parse_qs(urlparse(route.request.url).query)
        requested = query.get("provider", ["auto"])[0]
        calls.append(("snapshot", requested, None))
        route.fulfill(status=200, json={
            "available": True, "symbol": "BTC/EUR", "resolved_provider": requested,
            "price": "105", "bid": None, "ask": None,
            "market_time_status": "unknown", "received_at": datetime.now(timezone.utc).isoformat(),
        })

    page.route("**/market/candles?*", candles)
    page.route("**/market/snapshot?*", snapshot)
    page.route("**/paper/operations?*", lambda route: route.fulfill(status=200, json=[]))
    page.route("**/paper/portfolio?*", lambda route: route.fulfill(status=404, json={"detail": "none"}))

    page.goto(BASE + "#/estudio")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#st-meta")).to_contain_text("KRAKEN")
    assert ("snapshot", "kraken", None) in calls, calls
    calls.clear()
    page.locator("#st-tf").select_option("1M")
    expect(page.locator("#st-meta")).to_contain_text("BINANCE")
    expect(page.locator("#st-price-source")).to_contain_text("BINANCE")
    assert ("candles", "auto", "1M") in calls, calls
    assert ("snapshot", "binance", None) in calls, calls
    assert not any(kind == "snapshot" and provider == "auto" for kind, provider, _ in calls), calls
    assert page.evaluate("window.__orbisWsUrls.at(-1)").endswith("provider=binance")
    page.evaluate("""window.__orbisWs.at(-1).emit({available:true, price:'111',
        resolved_provider:'binance', market_time_status:'unknown',
        received_at:new Date().toISOString()})""")
    expect(page.locator("#st-meta")).to_contain_text("111")
    page.evaluate("""window.__orbisWs.at(-1).emit({available:true, price:'999',
        resolved_provider:'binance', market_time_status:'stale',
        received_at:new Date(Date.now()-120000).toISOString()})""")
    expect(page.locator("#st-meta")).to_contain_text("111")
    page.evaluate("""nextMs => { const oldNow=Date.now; Date.now=()=>nextMs;
        window.__orbisWs.at(-1).emit({available:true, price:'222',
          resolved_provider:'binance', market_time_status:'unknown',
          received_at:new Date(nextMs).toISOString()}); Date.now=oldNow; }""",
                  NEXT_MONTH * 1000)
    page.wait_for_timeout(100)
    assert "222" not in page.locator("#st-meta").inner_text()
    assert not errors, errors
    browser.close()
