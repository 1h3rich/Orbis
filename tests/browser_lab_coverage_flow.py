"""Un run verificado distingue señal ausente de indicador sin calentamiento."""

from datetime import datetime, timezone

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
rows = []
for index in range(16):
    close = 10 + index // 4 * 10
    rows.append({"t": start + index * 3600, "o": str(close),
                 "h": str(close + 1), "l": str(close - 1),
                 "c": str(close), "v": "1"})
definition = {"direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
              "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.9",
              "take_profit": "9", "entry": {"kind": "CLOSE_ABOVE_EMA",
                                               "period": 200, "timeframe": "4h"}}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    dataset = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "Cobertura fixture",
        "candles": rows})
    assert dataset.ok, dataset.text()
    version = page.request.post(BASE + "/lab/strategies", data={
        "name": "EMA 200 de 4h", "definition": definition})
    assert version.ok, version.text()
    run = page.request.post(BASE + "/lab/runs", data={
        "dataset_id": dataset.json()["id"], "version_id": version.json()["version_id"]})
    assert run.ok, run.text()
    assert run.json()["metrics"]["sample_size"] == 0
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run.json()["id"])
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "unchecked")
    assert page.locator("#lab-run-coverage").is_hidden()
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    expect(page.locator('#lab-run-coverage [data-status="INSUFFICIENT"]')).to_contain_text(
        "4h · 4/200 velas")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run.json()["id"])
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator('#lab-run-coverage [data-status="INSUFFICIENT"]')).to_contain_text(
        "4h · 4/200 velas")
    assert not errors, errors
    page.screenshot(path="/tmp/orbis-lab-coverage.png", full_page=True)
    browser.close()
    print("LAB coverage: verified insufficient warmup shown after reload")
