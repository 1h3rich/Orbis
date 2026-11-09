"""Controlled quotes exercise local alerts without credentials or external requests."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUT = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])
KEY = "orbis.market.alerts.v1"
LEGACY = "orbis.estudio.__al|all|x|alerts"
START = datetime.now(timezone.utc).replace(microsecond=0)
BTC = "BTC/EUR"
ETH = "ETH/USDT"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 950})
    page = context.new_page()
    page.clock.install(time=START)
    page.add_init_script(f"""(() => {{
      if (localStorage.getItem('{LEGACY}') !== null) return;
      localStorage.setItem('{LEGACY}', JSON.stringify([
        {{id:'legacy',symbol:'{BTC}',provider:'kraken',op:'>',price:'100'}},
      ]));
      localStorage.setItem('orbis.estudio.__al_fired|all|x|alerts', '{{"legacy":1}}');
      localStorage.setItem('orbis.alert.sound','false');
    }})();""")
    errors, writes, requested, deferred = [], [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    context.on("request", lambda request: writes.append(request.url) if request.method != "GET" else None)
    stamp = START
    quotes = {
        (BTC, "kraken"): {"price": "100.000000000000000001"},
        (BTC, "auto"): {"price": "100"},
        (ETH, "auto"): {"price": "3e-18"},
    }
    delay = None

    def market(route):
        parsed = urlparse(route.request.url)
        if not route.request.url.startswith(BASE + "/"):
            route.abort()
            return
        if not parsed.path.startswith("/market/"):
            route.continue_()
            return
        query = parse_qs(parsed.query)
        if parsed.path == "/market/snapshot":
            symbol, provider = query["symbol"][0], query["provider"][0]
            pair = (symbol, provider)
            requested.append(pair)
            payload = {
                "symbol": symbol, "requested_provider": provider,
                "resolved_provider": "mexc" if provider == "auto" else provider,
                "available": True, "received_at": stamp.isoformat(),
                "market_time_status": "unknown", "price": "10",
                **quotes.get(pair, {}),
            }
            if pair == delay:
                deferred.append((route, payload))
                return
        elif parsed.path == "/market/price":
            payload = {"price": "100", "source": "kraken", "age_seconds": 1}
        else:
            payload = {"available": False, "reason": "fixture sin velas"}
        route.fulfill(status=200, json=payload)

    context.route("**/*", market)
    context.route_web_socket("**/market/stream?**", lambda socket: socket.close())
    page.goto(BASE + "/ui/#/alertas")
    rules = page.locator("#alerts-rules tbody")
    history = page.locator("#alerts-events")
    expect(rules).to_contain_text("Condición cumplida")
    expect(history).to_contain_text("Sin avisos registrados")
    page.wait_for_function("key => JSON.parse(localStorage.getItem(key))?.rules[0]?.received", arg=KEY)

    def add(symbol, provider, op, price):
        form = page.locator("#alerts-create")
        form.locator('[name="symbol"]').fill(symbol)
        form.locator('[name="provider"]').select_option(provider)
        form.locator('[name="op"]').select_option(op)
        form.locator('[name="price"]').fill(price)
        form.get_by_role("button", name="Crear alerta").click()

    def state():
        return page.evaluate("key => JSON.parse(localStorage.getItem(key))", KEY)

    def tick():
        global stamp
        stamp += timedelta(seconds=21)
        page.clock.fast_forward(21000)

    add(BTC, "kraken", ">", "100,000000000000000001")
    expect(rules.get_by_role("row")).to_have_count(2)
    expect(rules.get_by_role("row").nth(1)).to_contain_text("Vigilando")
    add(ETH, "auto", "<", "0.000000000000000002")
    expect(rules.get_by_role("row")).to_have_count(3)
    expect(rules.get_by_role("row").nth(2)).to_contain_text("Vigilando")
    add(ETH, "auto", "<", "2e-18")
    expect(page.get_by_text("Ya existe esta alerta.", exact=True)).to_be_visible()
    expect(rules.get_by_role("row")).to_have_count(3)

    page.get_by_role("link", name="Inicio", exact=True).click()
    expect(page.locator("#home-alerts")).to_have_text("3 alertas en vigilancia")
    quotes[(BTC, "kraken")].update(price="200", resolved_provider="mexc")
    quotes[(ETH, "auto")].update(price="1e-18", market_time_status="stale")
    before = len(requested)
    tick()
    page.wait_for_function("key => JSON.parse(localStorage.getItem(key)).rules.every(r => r.unavailable)", arg=KEY)
    assert len(requested) - before == 2
    assert not state()["events"]

    # A decimal above the threshold by one last-place unit must fire.
    quotes[(BTC, "kraken")].update(price="100.000000000000000002", resolved_provider="kraken")
    quotes[(ETH, "auto")].update(price="1e-18", market_time_status="unknown")
    tick()
    expect(page.locator(".toast--alert")).to_have_count(2)
    expect(page.locator("#home-alerts")).to_contain_text("Último aviso")
    saved = state()["events"]
    assert {event["symbol"]: event["provider"] for event in saved} == {BTC: "kraken", ETH: "mexc"}
    assert all(event["quoteTimeKind"] == "received" for event in saved)
    assert all(event["quoteAt"] == stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z") for event in saved)

    # Staying beyond the threshold produces no new event. An incorrect pair
    # cannot rearm a rule. A valid equality rearms it.
    tick()
    page.wait_for_function("(p) => JSON.parse(localStorage.getItem(p.key)).rules[1].received === p.time",
                           arg={"key": KEY, "time": int(stamp.timestamp() * 1000)})
    assert state()["events"] == saved
    quotes[(BTC, "kraken")].update(price="1", symbol="BTC/USDT")
    tick()
    page.wait_for_function("key => JSON.parse(localStorage.getItem(key)).rules[1].unavailable", arg=KEY)
    assert state()["rules"][1]["active"]
    quotes[(BTC, "kraken")].update(price="100.000000000000000001", symbol=BTC)
    tick()
    page.wait_for_function("key => !JSON.parse(localStorage.getItem(key)).rules[1].active", arg=KEY)
    market_time = stamp + timedelta(seconds=20)
    quotes[(BTC, "kraken")].update(price="100.000000000000000002", market_time_status="fresh",
                                   market_time_at=market_time.isoformat(),
                                   event_time_ns=str(int(market_time.timestamp()) * 1_000_000_000))
    tick()
    page.wait_for_function("key => JSON.parse(localStorage.getItem(key)).events.length === 3", arg=KEY)
    assert state()["events"][0]["quoteTimeKind"] == "market"
    quotes[(BTC, "kraken")]["price"] = "99"
    tick()
    page.wait_for_function("(p) => JSON.parse(localStorage.getItem(p.key)).rules[2].received === p.time",
                           arg={"key": KEY, "time": int(stamp.timestamp() * 1000)})
    assert state()["rules"][1]["active"]  # Same exchange event, different receipt.
    quotes[(BTC, "kraken")].update(price="100.000000000000000002", market_time_status="unknown")

    # A concurrent tab observes the saved history and can remove a rule before
    # a pending quote for that rule is applied. The other tab must not revive it.
    other = context.new_page()
    other.clock.install(time=stamp)
    other.on("pageerror", lambda error: errors.append(str(error)))
    other.goto(BASE + "/ui/#/alertas")
    expect(other.locator("#alerts-events tbody tr")).to_have_count(3)
    expect(other.locator(".toast--alert")).to_have_count(0)
    other.bring_to_front()
    expect(other.get_by_role("heading", name="Alertas", exact=True)).to_be_visible()
    delay = (ETH, "auto")
    quotes[(ETH, "auto")]["price"] = "4e-18"
    tick()
    other.clock.fast_forward(21000)
    # Dispatch callbacks through Playwright's sync event loop.
    for _ in range(20):
        if deferred:
            break
        other.wait_for_timeout(50)
    assert len(deferred) == 1, deferred
    other.get_by_role("button", name="Quitar alerta ETH/USDT < 0.000000000000000002").click()
    expect(other.locator("#alerts-rules tbody tr")).to_have_count(2)
    delay = None
    for pending, payload in deferred:
        pending.fulfill(status=200, json=payload)
    expect(page.locator("#home-alerts")).to_contain_text("2 alertas en vigilancia")
    assert all(rule["symbol"] != ETH for rule in state()["rules"])
    expect(other.locator("#alerts-events tbody tr")).to_have_count(3)
    other.close()

    # Estudio uses the same local register, accepting decimal commas.
    page.goto(BASE + "/ui/#/estudio?market=BTC%2FEUR")
    page.get_by_role("button", name="Alertas de precio").click()
    estudio = page.locator("#stf-panel-alerts")
    estudio.locator("#st-alop").select_option("lt")
    estudio.locator("#st-alprice").fill("42,5")
    estudio.get_by_role("button", name="Añadir alerta").click()
    expect(estudio).to_contain_text("BTC/EUR < 42.5")
    estudio.get_by_role("link", name="Gestionar alertas").click()
    expect(page.locator("#alerts-rules tbody tr")).to_have_count(3)
    expect(page.locator("#alerts-events tbody tr")).to_have_count(3)
    page.reload()
    expect(page.locator("#alerts-events tbody tr")).to_have_count(3)
    expect(page.locator("#alerts-rules tbody tr")).to_have_count(3)
    assert page.evaluate(f"localStorage.getItem('{LEGACY}').includes('legacy')")
    page.get_by_role("link", name="Configuración", exact=True).click()
    page.get_by_role("button", name="Probar sonido").click()
    expect(page.get_by_text("Sonido de prueba reproducido.", exact=True)).to_be_visible()
    page.locator("#global-search-open").click()
    page.locator("#global-search-input").fill("umbral")
    page.get_by_role("option", name="Alertas").click()
    expect(page.get_by_role("heading", name="Alertas", exact=True)).to_be_visible()
    page.evaluate("document.querySelector('#toasts button')?.click()")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(OUT / "alertas-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("""() => document.documentElement.scrollWidth <= innerWidth + 1 &&
        [...document.querySelectorAll('.alert-table')].every(table => table.scrollWidth <= table.clientWidth + 1)""")
    expect(page.get_by_role("button", name="Quitar alerta BTC/EUR < 42.5")).to_be_visible()
    page.screenshot(path=str(OUT / "alertas-mobile.png"), full_page=True)
    assert not errors and not writes, (errors, writes)
    browser.close()
    print("Alertas: migración, varios mercados, precisión, rearme, pestañas, eliminación e historial local verificados.")
