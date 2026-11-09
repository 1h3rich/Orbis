"""Comprueba en Chromium que Estudio entrega las marcas PAPER en velas reales."""

from datetime import datetime, timedelta, timezone
import os
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


BASE = os.environ.get("ORBIS_BROWSER_BASE", "http://127.0.0.1:8765/ui/#/estudio")
START = datetime(2026, 10, 10, tzinfo=timezone.utc)
FIRST = int(START.timestamp())
ROWS = [{"t": FIRST + hour * 3600, "o": "100", "h": "102", "l": "99",
         "c": "101", "v": "10", "closed": True} for hour in range(72) if hour != 40]


def operation(hour, minute, side="BUY"):
    when = START + timedelta(hours=hour, minutes=minute)
    return {"timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
            "asset": "BTC", "quote_currency": "EUR", "operation_type": side,
            "asset_received": "0.01" if side == "BUY" else "100",
            "amount_spent": "100" if side == "BUY" else "0.01", "price": "10000",
            "trading_fee": "0", "withdrawal_fee": "0", "network_fee": "0"}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
      let library;
      window.__orbisMarkerTimes = [];
      window.__orbisMarkers = [];
      Object.defineProperty(window, 'LightweightCharts', {
        configurable: true,
        get() { return library; },
        set(value) {
          library = Object.assign({}, value);
          library.createChart = function (...args) {
            const chart = value.createChart(...args);
            window.__orbisChart = chart;
            return chart;
          };
          library.createSeriesMarkers = function (series, marks) {
            const api = value.createSeriesMarkers(series, marks);
            const original = api.setMarkers.bind(api);
            api.setMarkers = function (items) {
              window.__orbisMarkers = items;
              window.__orbisMarkerTimes = items.filter(item => item.color === '#3fb950')
                .map(item => item.time);
              return original(items);
            };
            return api;
          };
        }
      });
    })()""")
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "available": True, "symbol": "BTC/EUR", "resolved_provider": "kraken",
        "timeframe": "1h", "candles": ROWS, "older_pagination_supported": False,
    }))
    page.route("**/market/snapshot?*", lambda route: route.fulfill(status=200, json={
        "available": True, "resolved_provider": "kraken", "price": "101",
        "timestamp": ROWS[-1]["t"],
    }))
    paper_queries = []
    def paper_operations(route):
        paper_queries.append(parse_qs(urlsplit(route.request.url).query))
        route.fulfill(status=200, json=[
            operation(20, 10), operation(20, 50), operation(20, 55, "SELL"),
            operation(21, 20, "SELL"), operation(40, 30, "SELL"), operation(75, 0),
        ])
    page.route("**/paper/operations?*", paper_operations)
    page.route("**/strategies*", lambda route: route.fulfill(status=200, json=[]))
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    page.locator("#st-pair").fill("BTC/EUR")
    page.locator("#st-tf").select_option("1h")
    page.wait_for_function("expected => JSON.stringify(window.__orbisMarkerTimes) === JSON.stringify([expected])",
                           arg=FIRST + 20 * 3600, timeout=30000)
    markers = page.evaluate("window.__orbisMarkers")
    assert [mark["text"] for mark in markers if mark["time"] == FIRST + 20 * 3600] == ["P ×2", "S"]
    x = page.evaluate("time => window.__orbisChart.timeScale().timeToCoordinate(time)", FIRST + 20 * 3600)
    assert x is not None
    page.locator("#st-chart").scroll_into_view_if_needed()
    chart_box = page.locator("#st-chart").bounding_box()
    page.evaluate("window.__orbisChart.subscribeCrosshairMove(param => { window.__orbisCrossTime = param.time; })")
    page.mouse.move(chart_box["x"] + x, chart_box["y"] + 95)
    page.wait_for_timeout(300)
    debug = page.evaluate("({time:window.__orbisCrossTime,hidden:document.getElementById('st-marker-tip').hidden})")
    assert not debug["hidden"], {"x": x, "chart": chart_box, "crosshair": debug}
    tip = page.locator("#st-marker-tip").inner_text()
    assert "3 operaciones PAPER en esta vela" in tip
    assert "1 más en Movimientos" in tip
    tip_box = page.locator("#st-marker-tip").bounding_box()
    assert tip_box["y"] >= chart_box["y"] and tip_box["y"] + tip_box["height"] <= chart_box["y"] + chart_box["height"]
    sale_x = page.evaluate("time => window.__orbisChart.timeScale().timeToCoordinate(time)", FIRST + 21 * 3600)
    page.mouse.move(chart_box["x"] + sale_x, chart_box["y"] + 95)
    sale_tip = page.locator("#st-marker-tip").inner_text()
    assert "Venta PAPER" in sale_tip and "Cantidad 0,01 BTC" in sale_tip
    assert "Importe 100,00" in sale_tip
    assert "Cantidad 100 BTC" not in sale_tip
    blank_x = page.evaluate("time => window.__orbisChart.timeScale().timeToCoordinate(time)", FIRST + 22 * 3600)
    page.mouse.move(chart_box["x"] + blank_x, chart_box["y"] + 95)
    assert page.locator("#st-marker-tip").is_hidden()
    assert any(query.get("asset") == ["BTC"] and query.get("quote") == ["EUR"] and
               query.get("start_at") == [START.isoformat().replace("+00:00", ".000Z")] and
               query.get("end_before") == [(START + timedelta(hours=72)).isoformat().replace("+00:00", ".000Z")] and
               "limit" not in query for query in paper_queries), paper_queries
    assert not errors, errors
    browser.close()
    print("Estudio: marca PAPER en vela observada; hueco y fuera de cobertura sin marca")
