"""Chromium con la librería real y datos de mercado controlados."""

from bisect import bisect_left
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUTPUT = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])
DAY = 86400
FOUR_HOURS = DAY // 6
WEEK = DAY * 7
NOW = datetime.now(timezone.utc)
END = int(NOW.timestamp()) // FOUR_HOURS * FOUR_HOURS - FOUR_HOURS
FIRST = END - 2 * 365 * DAY
PAPER_BAR = END - 330 * DAY
GAP = PAPER_BAR + FOUR_HOURS
WEEK_END = int((NOW - timedelta(days=NOW.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
WEEK_FROM = WEEK_END - 12 * WEEK
ARCHIVE_START = int(datetime(2012, 1, 1, tzinfo=timezone.utc).timestamp())
ARCHIVE_START_EUR = int(datetime(2016, 4, 16, tzinfo=timezone.utc).timestamp())


def candle(stamp):
    return {"t": stamp, "o": "100", "h": "102", "l": "99", "c": "101", "v": "10", "closed": True}


def date(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()


def operation(stamp):
    return {"timestamp": datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "asset": "BTC", "quote_currency": "EUR", "operation_type": "BUY",
            "asset_received": "0.01", "amount_spent": "100", "price": "10000",
            "trading_fee": "0", "withdrawal_fee": "0", "network_fee": "0"}


archive_times = list(range(ARCHIVE_START, END - END % DAY + 1, DAY))
# 1 año en Inicio llega hasta la última página disponible, a 250 días.
daily_times = archive_times[-250:]
data = {"4h": ([stamp for stamp in range(FIRST, END + 1, FOUR_HOURS) if stamp != GAP], 400),
        "1w": (list(range(WEEK_END - 105 * WEEK, WEEK_END + 1, WEEK)), 70),
        "1d": (daily_times, 100)}
requests = []
errors = []


def market(route):
    query = {key: values[0] for key, values in parse_qs(urlsplit(route.request.url).query).items()}
    requests.append(query)
    interval = query["timeframe"]
    times, size = data.get(interval, data["4h"])
    stop = bisect_left(times, int(query["before"])) if "before" in query else len(times)
    start = max(0, stop - size)
    route.fulfill(json={"available": True, "symbol": query["symbol"], "resolved_provider": "binance",
                        "timeframe": interval, "candles": [candle(t) for t in times[start:stop]],
                        "older_pagination_supported": True, "history_exhausted": start == 0})


with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
      let library;
      window.WebSocket = class { addEventListener() {} close() {} };
      window.studyPaperTimes = [];
      Object.defineProperty(window, 'LightweightCharts', {
        configurable: true,
        get() { return library; },
        set(value) {
          library = {...value};
          library.createChart = function (...args) {
            const chart = value.createChart(...args);
            if (args[0].id === 'st-chart') {
              window.studyChart = chart;
              const scale = chart.timeScale();
              const setRange = scale.setVisibleRange.bind(scale);
              scale.setVisibleRange = range => {
                window.studyRequestedRange = range;
                return setRange(range);
              };
            }
            return chart;
          };
          library.createSeriesMarkers = function (series, marks) {
            const plugin = value.createSeriesMarkers(series, marks);
            const set = plugin.setMarkers.bind(plugin);
            plugin.setMarkers = function (items) {
              window.studyPaperTimes = items.filter(mark => mark.color === '#3fb950').map(mark => mark.time);
              return set(items);
            };
            return plugin;
          };
        }
      });
    })()""")
    page.route("**/market/**", lambda route: route.fulfill(json={}))
    page.route("**/market/candles?*", market)
    def archive(route):
        quote = parse_qs(urlsplit(route.request.url).query)["quote_currency"][0]
        times = archive_times if quote == "USD" else archive_times[bisect_left(archive_times, ARCHIVE_START_EUR):]
        route.fulfill(json={
            "available": True, "resolved_provider": "bitstamp", "timeframe": "1d",
            "first_candle_at": times[0], "last_candle_at": times[-1],
            "missing_days": 0, "candles": [candle(t) for t in times],
        })

    page.route("**/market/btc-daily-archive?*", archive)
    page.route("**/market/snapshot?*", lambda route: route.fulfill(json={
        "available": True, "resolved_provider": "binance", "price": "101", "timestamp": END,
    }))
    page.route("**/paper/operations?*", lambda route: route.fulfill(json=[
        operation(PAPER_BAR + 3600), operation(GAP + 3600),
    ]))
    page.route("**/strategies*", lambda route: route.fulfill(json=[]))
    page.goto(BASE + "/ui/#/estudio")
    chart = page.locator("#st-chart")
    interval = page.locator("#st-tf")
    period = page.locator("#st-range")
    page.locator("#st-pair").fill("BTC/EUR")
    interval.select_option("4h")
    period.select_option("1")
    expect(chart).to_have_attribute("data-archive-window", "1")
    expect(interval).to_have_value("4h")
    page.wait_for_function("stamp => window.studyPaperTimes.includes(stamp)", arg=PAPER_BAR)
    assert page.evaluate("window.studyPaperTimes") == [PAPER_BAR]
    visible = page.evaluate("window.studyChart.timeScale().getVisibleRange()")
    from_time, to_time = visible["from"], visible["to"]
    assert to_time == END and to_time - from_time >= 364 * DAY, (from_time, to_time)
    older = [q for q in requests if q["timeframe"] == "4h" and "before" in q]
    assert len(older) >= 5
    assert all(q["provider"] == "binance" and q["symbol"] == "BTC/EUR" for q in older)
    cursors = [int(q["before"]) for q in older]
    assert cursors == sorted(set(cursors), reverse=True)

    interval.select_option("1w")
    expect(period).to_have_value("1")
    expect(chart).to_have_attribute("data-archive-window", "1")
    period.select_option("custom")
    page.locator("#st-range-from").fill(date(WEEK_FROM + 2 * DAY))
    page.locator("#st-range-to").fill(date(WEEK_FROM + WEEK + 3 * DAY))
    page.locator("#st-range-apply").click()
    expect(chart).to_have_attribute("data-archive-window", "custom")
    weekly = page.evaluate("window.studyRequestedRange")
    assert weekly == {"from": WEEK_FROM, "to": WEEK_FROM + WEEK}, weekly
    expect(interval).to_have_value("1w")

    page.locator("#st-pair").fill("BTC/USD")
    page.locator("#st-pair").press("Enter")
    interval.select_option("1d")
    period.select_option("all")
    expect(chart).to_have_attribute("data-archive-window", "all")
    expect(chart).to_have_attribute("data-archive-from", str(ARCHIVE_START))
    page.goto(BASE + "/ui/#/")
    page.locator("#home-range").select_option("1y")
    expect(page.locator("#home-market")).to_contain_text("250 puntos")

    assert not errors, errors
    (OUTPUT / "result.json").write_text(json.dumps({
        "native_interval": "4h", "coverage_days": (to_time - from_time) / DAY,
        "older_pages": len(older), "old_paper_marker": True, "no_marker_in_gap": True,
        "weekly_custom_overlap": True, "daily_btc_archive": date(ARCHIVE_START),
        "home_nonempty_final_page": True,
    }))
    browser.close()
