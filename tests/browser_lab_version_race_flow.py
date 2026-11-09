"""Una respuesta antigua de versiones no cambia la estrategia ni el run actual."""

import json

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
PASSWORD = "browser lab version race fixture"
START = 1735689600


def definition(direction):
    return {"direction": direction, "initial_capital": "1000", "allocation": "1",
            "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
            "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    created = page.request.post(BASE + "/lab/create", data={"passphrase": PASSWORD})
    assert created.ok and created.json()["unlocked"], created.text()
    dataset = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "fixture-version-race",
        "candles": [{"t": START + i * 3600, "o": str(10 + i),
                     "h": str(11 + i), "l": str(9 + i), "c": str(10 + i), "v": "10"}
                    for i in range(6)]})
    assert dataset.ok, dataset.text()
    a = page.request.post(BASE + "/lab/strategies", data={
        "name": "Estrategia A", "definition": definition("LONG")})
    b = page.request.post(BASE + "/lab/strategies", data={
        "name": "Estrategia B", "definition": definition("LONG")})
    assert a.ok and b.ok, (a.text(), b.text())
    a, b = a.json(), b.json()
    b2 = page.request.post(BASE + "/lab/strategies/" + b["id"] + "/versions",
                           data={"definition": definition("SHORT")})
    assert b2.ok, b2.text()
    b2 = b2.json()
    page.add_init_script("""
        const realFetch = window.fetch.bind(window);
        const aNeedle = "/lab/strategies/" + %s + "/versions";
        const bNeedle = "/lab/strategies/" + %s + "/versions";
        window.__delayNextB = false;
        window.fetch = function(input, options) {
          const url = typeof input === "string" ? input : input.url;
          if (url === aNeedle) return new Promise((resolve, reject) => {
            window.__releaseA = () => realFetch(input, options).then(resolve, reject);
          });
          if (url === bNeedle && window.__delayNextB) {
            window.__delayNextB = false;
            return new Promise((resolve, reject) => {
              window.__releaseOldB = () => realFetch(input, options).then(resolve, reject);
            });
          }
          return realFetch(input, options);
        };
    """ % (json.dumps(a["id"]), json.dumps(b["id"])))
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(a["id"])
    page.wait_for_function("typeof window.__releaseA === 'function'")
    expect(page.locator("#lab-run")).to_be_disabled()
    expect(page.locator("#lab-version")).to_have_value("")
    page.locator("#lab-strategy").select_option(b["id"])
    expect(page.locator("#lab-version")).to_have_value(b2["version_id"])
    expect(page.locator("#lab-run")).to_be_enabled()
    page.evaluate("window.__releaseA()")
    expect(page.locator("#lab-version")).to_have_value(b2["version_id"])
    expect(page.locator('#lab-strategy-form [name="direction"]')).to_have_value("SHORT")

    page.evaluate("window.__delayNextB = true")
    page.locator("#lab-version").select_option(b["version_id"])
    page.wait_for_function("typeof window.__releaseOldB === 'function'")
    expect(page.locator("#lab-run")).to_be_disabled()
    page.locator("#lab-version").select_option(b2["version_id"])
    expect(page.locator("#lab-run")).to_be_enabled()
    page.evaluate("window.__releaseOldB()")
    expect(page.locator("#lab-version")).to_have_value(b2["version_id"])
    expect(page.locator('#lab-strategy-form [name="direction"]')).to_have_value("SHORT")

    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    run_id = page.locator("#lab-run-select").input_value()
    run = page.request.get(BASE + "/lab/runs/" + run_id)
    assert run.ok and run.json()["version_id"] == b2["version_id"], run.text()
    assert not errors, errors
    browser.close()
    print("LAB versions: stale responses ignored; run uses visible version")
