"""El informe LAB advierte cuando OHLC no determina si ganó stop u objetivo."""

from playwright.sync_api import sync_playwright


START = 1735689600
STEP = 3600
rows = [
    {"t": START + i * STEP, "o": str(10 + i), "h": str(11 + i),
     "l": str(9 + i), "c": str(10 + i), "v": "10"}
    for i in range(5)
]
rows[2].update(h="20", l="5")
definition = {
    "direction": "LONG", "initial_capital": "1000", "allocation": "1",
    "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
    "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2},
}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    base = "http://127.0.0.1:8799"
    dataset_response = page.request.post(base + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "fixture-ambiguity", "candles": rows,
    })
    assert dataset_response.ok, dataset_response.text()
    strategy_response = page.request.post(base + "/lab/strategies", data={
        "name": "Ambigüedad OHLC", "definition": definition,
    })
    assert strategy_response.ok, strategy_response.text()
    run_response = page.request.post(base + "/lab/runs", data={
        "dataset_id": dataset_response.json()["id"],
        "version_id": strategy_response.json()["version_id"],
    })
    assert run_response.ok, run_response.text()
    run = run_response.json()
    assert run["metrics"]["ambiguous_exits"] == 1
    page.goto(base + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run["id"])
    warning = page.locator("#lab-report p.amber").filter(has_text="salida ambigua")
    warning.wait_for()
    assert "se asumió stop" in warning.inner_text()
    assert page.locator("#lab-report tbody tr").filter(has_text="STOP_AND_TARGET_AMBIGUOUS").count() == 1
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run["id"])
    page.locator("#lab-report p.amber").filter(has_text="salida ambigua").wait_for()
    assert not errors, errors
    browser.close()
    print("Browser LAB ambiguity flow passed")
