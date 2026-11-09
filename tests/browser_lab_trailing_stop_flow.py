"""LAB: edición, backtest y recuperación del stop móvil sin habilitar PAPER."""

from playwright.sync_api import expect, sync_playwright


start = 1_735_689_600
rows = [(100, 100, 100, 100), (110, 110, 110, 110),
        (100, 120, 95, 110), (110, 112, 107, 109)]
csv = "time,open,high,low,close,volume\n" + "\n".join(
    f"{start + i * 3600},{o},{h},{l},{c},10" for i, (o, h, l, c) in enumerate(rows))

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-csv [name="file"]').set_input_files({
        "name": "trailing.csv", "mimeType": "text/csv", "buffer": csv.encode()})
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-strategy-form [name="name"]').fill("Stop móvil causal")
    page.locator('#lab-rule-list [name="kind"]').first.select_option("CLOSE_ABOVE_SMA")
    page.locator('#lab-rule-list [name="period"]').first.fill("2")
    page.locator('#lab-strategy-form [name="fee_rate"]').fill("0")
    page.locator('#lab-strategy-form [name="slippage_rate"]').fill("0")
    page.locator('#lab-strategy-form [name="stop_loss"]').fill("0.1")
    page.locator('#lab-strategy-form [name="take_profit"]').fill("0.5")
    page.locator('#lab-strategy-form [name="trailing_stop"]').fill("0.1")
    assert "Spot PAPER" in page.locator('#lab-strategy-form [name="trailing_stop"]').locator("xpath=..").get_attribute("data-help")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-strategy").select_option(index=1)
    assert page.locator('#lab-strategy-form [name="trailing_stop"]').input_value() == "0.1"
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    expect(page.locator("#lab-report tbody tr").first).to_contain_text("Stop móvil")
    run_id = page.locator("#lab-run-select").input_value()
    run = page.request.get("http://127.0.0.1:8799/lab/runs/" + run_id).json()
    assert run["engine_version"] == "lab-1.16"
    assert run["trades"][0]["reason"] == "TRAILING_STOP"
    assert run["trades"][0]["exit_price"] == "108.0"
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run-select").select_option(run_id)
    assert page.locator('#lab-strategy-form [name="trailing_stop"]').input_value() == "0.1"
    assert page.request.get("http://127.0.0.1:8799/lab/runs/" + run_id + "/verify").json()["verified"]
    assert not errors, errors
    browser.close()
    print("LAB trailing stop: editor, backtest, restart and verification OK")
