"""Comparación LAB verificada y rechazo visual de históricos distintos."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
START = 1735689600
STEP = 3600


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def post(path, payload):
        response = page.request.post(BASE + path, data=payload)
        assert response.status == 200, response.text()
        return response.json()

    candles = [{"t": START + i * STEP, "o": str(100 + i), "h": str(102 + i),
                "l": str(99 + i), "c": str(101 + i), "v": "10"}
               for i in range(80)]
    dataset = post("/lab/datasets", {"symbol": "BTC/USDT", "timeframe": "1h",
                                     "source": "CSV local", "candles": candles})
    definition = {"direction": "LONG", "initial_capital": "1000", "allocation": "1",
                  "fee_rate": "0.001", "slippage_rate": "0.001", "stop_loss": "0.03",
                  "take_profit": "0.08", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}
    first = post("/lab/strategies", {"name": "Tendencia", "definition": definition})
    second = post(f"/lab/strategies/{first['id']}/versions",
                  {"definition": {**definition, "take_profit": "0.12"}})
    left = post("/lab/runs", {"dataset_id": dataset["id"], "version_id": first["version_id"]})
    right = post("/lab/runs", {"dataset_id": dataset["id"], "version_id": second["version_id"]})
    other_candles = [{**bar, "o": str(200 + i), "h": str(202 + i),
                      "l": str(199 + i), "c": str(201 + i)} for i, bar in enumerate(candles)]
    other_dataset = post("/lab/datasets", {"symbol": "BTC/USDT", "timeframe": "1h",
                                           "source": "CSV local", "candles": other_candles})
    other_run = post("/lab/runs", {"dataset_id": other_dataset["id"],
                                   "version_id": first["version_id"]})

    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-compare-left").select_option(left["id"])
    page.locator("#lab-compare-right").select_option(right["id"])
    page.locator("#lab-compare-action").click()
    expect(page.locator("#lab-comparison")).to_contain_text("VERIFICADOS")
    expect(page.locator("#lab-comparison")).to_contain_text("Rentabilidad")
    expect(page.locator("#lab-comparison")).to_contain_text("Δ alt. − base")
    page.locator("#lab-comparison").scroll_into_view_if_needed()
    page.screenshot(path="/tmp/orbis-lab-comparison.png", full_page=False)

    page.locator("#lab-compare-right").select_option(other_run["id"])
    page.locator("#lab-compare-action").click()
    expect(page.locator("#lab-comparison")).to_contain_text("datasets distintos")
    assert page.locator("#lab-comparison table").count() == 0
    assert not errors, errors
    browser.close()
    print("LAB: comparación verificada y datasets distintos")
