"""Inicio keeps each local layout and resizes its real chart without remounting."""
import os
from pathlib import Path
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUT = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])
HOUR = int(time.time()) // 3600 * 3600
KEY = "orbis.home.workspaces.v1"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors, histories, writes = [], [], []
    page.on("pageerror",lambda error:errors.append(str(error)))
    page.on("request",lambda request:writes.append(request.url) if request.method != "GET" else None)
    page.add_init_script("""(() => {
      window.__homeChart = null;
      Object.defineProperty(window, "LightweightCharts", {
        configurable:true,
        set(library) {
          const create = library.createChart;
          Object.defineProperty(window, "LightweightCharts", {value:{...library, createChart(host, options) {
            const chart = create(host, options);
            if (host.classList.contains("lwc-host") || host.closest("#home-market")) window.__homeChart = chart;
            return chart;
          }},configurable:true});
        }
      });
    })();""")

    def market(route):
        parsed = urlparse(route.request.url)
        query = parse_qs(parsed.query)
        if parsed.path == "/market/price":
            payload = {"price":"123","source":"kraken","age_seconds":1,"stale":False}
        elif parsed.path == "/market/history":
            histories.append(query)
            size = 169 if query["timeframe"] == ["7d"] else 25
            payload = {"points":[{"t":HOUR-(size-1-index)*3600,"open":"100","high":"103",
                "low":"98","close":str(100+index%3)} for index in range(size)],
                "source":"kraken","stale":False}
        else:
            payload = {}
        route.fulfill(status=200,json=payload)

    page.route("**/market/**",market)
    page.goto(BASE+"/ui/#/inicio")
    page.wait_for_load_state("networkidle")
    picker = page.locator("#home-workspace")
    editor = page.locator("#home-workspace-editor")
    grid = page.locator("#home-panel-grid")
    market_panel = grid.locator('[data-home-panel="market"]')
    quote = page.locator("#home-quote")
    chart = page.locator("#home-chart-cv canvas").first

    def order():
        return grid.locator(":scope > section").evaluate_all("nodes => nodes.map(node => node.dataset.homePanel)")

    expect(picker).to_have_value("general")
    assert order() == ["paper","market","real","watch","strategies","activity","dca","alerts"]
    expect(grid.locator(":scope > section:visible")).to_have_count(8)
    expect(chart).to_be_visible()
    with page.expect_request("**/market/history?*"):
        page.locator("#home-range").select_option("7d")
    expect(chart).to_be_visible()
    page.wait_for_function("window.__homeChart.timeScale().getVisibleLogicalRange().to > 100")
    page.evaluate("""() => {
      window.__rememberChart = window.__homeChart;
      window.__homeChart.timeScale().setVisibleLogicalRange({from:45,to:92});
    }""")

    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    page.evaluate("() => window.__originalRange = window.__homeChart.timeScale().getVisibleRange()")
    editor.locator("summary").click()
    editor.get_by_role("checkbox",name="Mostrar Mercados seguidos").uncheck()
    expect(grid.locator('[data-home-panel="watch"]')).to_be_hidden()
    move = editor.get_by_role("button",name="Subir Gráfico BTC")
    move.focus()
    move.press("Enter")
    assert order()[0] == "market"
    expect(editor.get_by_role("button",name="Subir Gráfico BTC")).to_be_disabled()
    assert editor.evaluate("(element) => element.contains(document.activeElement)")
    wide = editor.get_by_role("button",name="Ancho completo para Gráfico BTC")
    wide.click()
    expect(wide).to_have_attribute("aria-pressed","false")
    expect(market_panel).to_have_attribute("data-home-width","half")
    page.wait_for_function("document.querySelector('#home-market-panel').clientWidth < document.querySelector('#home-panel-grid').clientWidth * .6")
    wide.click()
    expect(market_panel).to_have_attribute("data-home-width","full")

    picker.select_option("portfolio")
    expect(market_panel).to_be_hidden()
    assert order()[0] == "real"
    picker.select_option("general")
    expect(chart).to_be_visible()
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    range_snapshot = page.evaluate("""() => ({
      sameChart:window.__homeChart === window.__rememberChart,
      actual:window.__homeChart.timeScale().getVisibleRange(),
      original:window.__originalRange
    })""")
    assert range_snapshot["sameChart"] and range_snapshot["actual"] == range_snapshot["original"],range_snapshot
    assert order()[0] == "market"

    picker.select_option("research")
    for title in ("Mercados seguidos","Planes PAPER","Alertas"):
        editor.get_by_role("checkbox",name="Mostrar "+title).uncheck()
    expect(editor.get_by_role("checkbox",name="Mostrar Gráfico BTC")).to_be_disabled()
    editor.get_by_role("button",name="Restablecer espacio").click()
    expect(editor.get_by_role("checkbox",name="Mostrar Mercados seguidos")).to_be_checked()

    picker.select_option("portfolio")
    history_count = len(histories)
    quote.select_option("USDT")
    page.wait_for_load_state("networkidle")
    assert len(histories) == history_count
    page.reload()
    expect(picker).to_have_value("portfolio")
    expect(market_panel).to_be_hidden()
    expect(quote).to_have_value("USDT")
    page.wait_for_load_state("networkidle")
    assert len(histories) == history_count
    with page.expect_request(lambda request: "/market/history?" in request.url and "quote_currency=USDT" in request.url):
        picker.select_option("general")
    expect(chart).to_be_visible()
    expect(page.locator("#home-range")).to_have_value("7d")
    assert order()[0] == "market"
    expect(grid.locator('[data-home-panel="watch"]')).to_be_hidden()

    editor.locator("summary").click()
    editor.screenshot(path=str(OUT/"home-workspace-desktop.png"))
    page.set_viewport_size({"width":390,"height":844})
    editor.screenshot(path=str(OUT/"home-workspace-mobile.png"))
    if not page.evaluate("document.documentElement.scrollWidth <= innerWidth+1"):
        raise AssertionError(page.evaluate("""() => Array.from(document.querySelectorAll('body *')).filter(node => {
          const rect = node.getBoundingClientRect();
          return rect.width && rect.right > innerWidth + 1;
        }).slice(0, 24).map(node => [node.tagName, node.id, node.className, node.getBoundingClientRect().right])"""))
    preferences = page.evaluate("(key) => JSON.parse(localStorage.getItem(key))",KEY)
    assert set(preferences) == {"active","layouts"}
    assert set(preferences["layouts"]) == {"general","trading","research","portfolio"}
    assert all(set(row) == {"id","visible","wide"} for layout in preferences["layouts"].values() for row in layout)
    page.evaluate("""(key) => localStorage.setItem(key, JSON.stringify({
      active:{toString:"untrusted"}, layouts:{general:[
        {id:"unknown",visible:true},{id:"market",visible:true,wide:"bad"},{id:"market",visible:false}]}
    }))""",KEY)
    page.reload()
    expect(picker).to_have_value("general")
    assert order()[0] == "market" and len(set(order())) == 8
    expect(market_panel).to_have_attribute("data-home-width","full")
    expect(grid.locator('[data-home-panel="alerts"]')).to_be_visible()
    assert not writes and not errors,(writes,errors)
    browser.close()
    print("Inicio: configuración por espacio, reinicio, foco, anchura y gráfico verificados")
