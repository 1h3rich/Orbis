"""Estructura de giros: editor, señal causal, entrada y recuperación."""

from datetime import datetime, timezone
from decimal import Decimal

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
highs = {3: "105", 9: "110", 15: "115"}
lows = {6: "90", 12: "94", 18: "98"}
rows = []
for index in range(27):
    base = Decimal("100") + Decimal(index) / 10
    rows.append({"t": start + index * 3600, "o": str(base), "c": str(base),
                 "h": highs.get(index, str(base + 1)),
                 "l": lows.get(index, str(base - 1)), "v": "10"})

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    response = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h",
        "source": "Giros confirmados fixture", "candles": rows})
    assert response.ok, response.text()
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-strategy-form [name="name"]').fill("Tres giros ascendentes")
    page.locator('#lab-rule-list [name="kind"]').select_option("STRUCTURE_HH_HL")
    page.locator('#lab-rule-list [name="period"]').fill("25")
    page.locator('#lab-rule-list [name="pivot_bars"]').fill("2")
    assert page.locator('#lab-rule-list [name="pivot_bars"]').is_visible()
    assert not page.locator('#lab-rule-list [name="value"]').is_visible()
    page.locator('#lab-strategy-form [name="stop_loss"]').fill("0.9")
    page.locator('#lab-strategy-form [name="take_profit"]').fill("9")
    page.locator('#lab-strategy-form [name="max_holding_bars"]').fill("2")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-chart canvas").first.wait_for()
    assert page.locator("#lab-chart").get_attribute("data-signal-count") == "1"
    assert page.locator("#lab-chart").get_attribute("data-entry-count") == "1"
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    version = page.request.get(BASE + "/lab/strategies").json()[0]
    assert version["definition"]["entry"]["children"][0] == {
        "kind": "STRUCTURE_HH_HL", "period": 25, "pivot_bars": 2}
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    assert page.locator('#lab-rule-list [name="kind"]').input_value() == "STRUCTURE_HH_HL"
    assert page.locator('#lab-rule-list [name="pivot_bars"]').input_value() == "2"
    page.locator("#lab-run-select").select_option(index=1)
    page.locator("#lab-chart canvas").first.wait_for()
    assert not errors, errors
    browser.close()
    print("LAB swing structure: editor, signal, entry marker, verification and reopen passed")
