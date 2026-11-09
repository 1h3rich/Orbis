"""Recorrido DCA sobre datos y bases temporales, sin tocar el patrimonio."""

from datetime import datetime, timezone
import json
import os
import re
import sqlite3

from playwright.sync_api import expect, sync_playwright


start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
prices = (100, 90, 80, 70, 75, 85)
candles = [
    {"t": start + i * 86400, "o": str(price), "h": str(price + 1),
     "l": str(price - 1), "c": str(price), "v": "10"}
    for i, price in enumerate(prices)
]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    imported = page.request.post("http://127.0.0.1:8799/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1d", "source": "browser-fixture", "candles": candles,
    })
    assert imported.ok, imported.text()
    page.goto("http://127.0.0.1:8799/ui/#/dca")
    page.wait_for_load_state("networkidle")
    page.locator("#dca-rule").wait_for()
    page.locator('#dca-form [name="name"]').fill("DCA comprobación")
    page.locator('#dca-form [name="frequency"]').select_option("daily")
    page.locator('#dca-form [name="budget_total"]').fill("1000")
    page.locator('#dca-form [name="monthly_limit"]').fill("150")
    page.locator('#dca-form [name="max_exposure"]').fill("1")
    page.locator("#dca-form .dca-options summary").click()
    page.locator('#dca-form [name="reference_mode"]').select_option("high_watermark")
    page.locator('#dca-form [name="fee_rate"]').fill("0")
    page.locator('#dca-form [name="slippage_rate"]').fill("0")
    page.locator("#dca-save").click()
    page.get_by_text("Plan guardado", exact=False).wait_for()
    assert page.locator('#dca-form [name="reference_mode"]').input_value() == "high_watermark"
    page.locator("#dca-version option").first.wait_for(state="attached")
    page.locator("#dca-run").click()
    page.get_by_text("Comparación guardada", exact=False).wait_for()
    expect(page.locator("#dca-report")).to_contain_text("referencia máximo observado")
    comparison = page.locator("#dca-report .tablewrap").first
    expect(comparison.locator("thead")).to_contain_text("Rent. capital")
    expect(comparison.locator("thead")).to_contain_text("Vol./vela")
    fixed = comparison.locator("tbody tr").filter(has_text="DCA fijo")
    expect(fixed.locator("td").nth(1)).to_contain_text("850")
    assert fixed.locator("td").nth(6).inner_text() not in ("—", "—%")
    assert fixed.locator("td").nth(9).inner_text() not in ("—", "—%")
    expect(fixed.locator("td").nth(9)).to_have_attribute("title", re.compile("muestra pequeña"))
    lump = comparison.locator("tbody tr").filter(has_text="Compra inicial")
    expect(lump.locator("td").first).to_have_attribute("title", "1000")
    page.locator("#dca-chart canvas").first.wait_for()
    page.locator("#dca-report .lab-verify button").click()
    expect(page.locator("#dca-report .lab-verify")).to_have_attribute("data-state", "verified")
    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile.goto("http://127.0.0.1:8799/ui/#/dca")
    mobile.wait_for_load_state("networkidle")
    mobile.locator("#dca-run-select").select_option(index=1)
    mobile.locator("#dca-chart canvas").first.wait_for()
    assert not mobile.evaluate("document.documentElement.scrollWidth > innerWidth")
    mobile.close()
    page.locator("#dca-chart-range select").select_option("5")
    assert page.locator("#dca-chart-range").get_attribute("data-view") == "5"
    assert "Cobertura parcial" in page.locator("#dca-chart-range").inner_text()
    assert int(page.locator("#dca-chart").get_attribute("data-execution-count")) > 0
    assert int(page.locator("#dca-chart").get_attribute("data-decision-count")) > 0
    assert page.locator("#dca-report tbody tr").count() >= 4
    page.screenshot(path="/tmp/orbis-dca-flow.png", full_page=True)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#dca-rule").select_option(index=1)
    assert page.locator('#dca-form [name="reference_mode"]').input_value() == "high_watermark"
    page.locator("#dca-run-select").select_option(index=1)
    page.locator("#dca-chart canvas").first.wait_for()
    expect(page.locator("#dca-chart-range")).to_have_attribute("data-view", "5")
    page.locator("#dca-chart-range select").select_option("custom")
    page.locator("#dca-chart-range [data-range-from]").fill("2025-01-02")
    page.locator("#dca-chart-range [data-range-to]").fill("2025-01-04")
    page.locator("#dca-chart-range [data-range-apply]").click()
    expect(page.locator("#dca-chart-range")).to_have_attribute("data-view", "custom")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#dca-run-select").select_option(index=1)
    page.locator("#dca-chart canvas").first.wait_for()
    expect(page.locator("#dca-chart-range")).to_have_attribute("data-view", "custom")
    assert page.locator("#dca-chart-range [data-range-from]").input_value() == "2025-01-02"
    page.evaluate("localStorage.setItem('orbis.chartRange.dca', JSON.stringify({mode:'custom',from:'2030-01-01',to:'2030-01-31'}))")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#dca-run-select").select_option(index=1)
    page.locator("#dca-chart canvas").first.wait_for()
    expect(page.locator("#dca-chart-range")).to_have_attribute("data-view", "all")
    expect(page.locator("#dca-chart-range")).to_contain_text("Fechas guardadas fuera de la cobertura")
    run_id = page.locator("#dca-report").get_attribute("data-run-id")
    with sqlite3.connect(os.environ["ORBIS_AUTOMATION_PATH"]) as db:
        stored = json.loads(db.execute("SELECT result_json FROM dca_runs WHERE id=?", (run_id,)).fetchone()[0])
        stored["results"]["conditional"]["metrics"]["invested"] = "999999"
        db.execute("UPDATE dca_runs SET result_json=? WHERE id=?", (json.dumps(stored), run_id))
    page.locator("#dca-report .lab-verify button").click()
    expect(page.locator("#dca-report .lab-verify")).to_have_attribute("data-state", "mismatch")
    expect(page.locator("#dca-chart")).to_contain_text("Gráfico bloqueado")
    assert page.locator("#dca-chart canvas").count() == 0
    assert not errors, errors
    print("Browser DCA flow passed: cash-aware comparison, replay, tamper alert, markers, recovery")
    browser.close()
