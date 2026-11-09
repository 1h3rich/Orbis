"""La tabla DCA muestra dinero grande sin pasar por Number."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
START = 1735689600
BUDGET = "123456789012345678.12"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    dataset = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1d", "source": "browser-decimal-fixture",
        "candles": [
            {"t": START + i * 86400, "o": "100", "h": "101", "l": "99",
             "c": "100", "v": "10"} for i in range(3)
        ],
    })
    assert dataset.ok, dataset.text()
    rule = page.request.post(BASE + "/automation/dca/rules", data={
        "name": "DCA precisión grande",
        "definition": {"frequency": "daily", "amount": "100", "budget_total": BUDGET,
                       "monthly_limit": "1000", "max_exposure": "1",
                       "fee_rate": "0", "slippage_rate": "0",
                       "max_extra_activations": 0, "cooldown_bars": 0, "dips": []},
    })
    assert rule.ok, rule.text()
    run = page.request.post(BASE + "/automation/dca/runs", data={
        "dataset_id": dataset.json()["id"], "version_id": rule.json()["version_id"],
    })
    assert run.ok, run.text()
    page.goto(BASE + "/ui/#/dca")
    page.wait_for_load_state("networkidle")
    page.locator("#dca-run-select").select_option(run.json()["id"])
    fixed = page.locator("#dca-report tbody tr").filter(has_text="DCA fijo").first
    expected_cash = "123456789012345478.12"
    expect(fixed.locator("td").nth(1)).to_have_attribute("title", expected_cash)
    expect(fixed.locator("td").nth(1)).to_contain_text("123.456.789.012.345.478,12")
    assert page.evaluate("window.OrbisUI.fmtDecimal('123456789012345478.12', 0, 2)") == \
        "123.456.789.012.345.478,12"
    assert not errors, errors
    browser.close()

print("DCA: importes grandes exactos en la tabla")
