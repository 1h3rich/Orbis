"""Editor LAB: límite de permanencia, backtest, marcas y recuperación."""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect, sync_playwright


start = 1735689600
csv = "time,open,high,low,close,volume\n" + "\n".join(
    f"{start + i * 3600},{10+i},{11+i},{9+i},{10+i},10" for i in range(8)
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    base = "http://127.0.0.1:8799"
    status = page.request.get(base + "/lab/status")
    assert status.ok and status.json()["format"] == "missing", status.text()
    created = page.request.post(base + "/lab/create", data={
        "passphrase": "browser time-limit fixture password"})
    assert created.ok and created.json()["unlocked"], created.text()
    page.goto(base + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-csv [name="file"]').set_input_files({
        "name": "velas.csv", "mimeType": "text/csv", "buffer": csv.encode()})
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-strategy-form [name="name"]').fill("Permanencia dos velas")
    page.locator('#lab-strategy-form [name="period"]').first.fill("2")
    page.locator('#lab-strategy-form [name="stop_loss"]').fill("0.9")
    page.locator('#lab-strategy-form [name="take_profit"]').fill("9")
    page.locator('#lab-strategy-form [name="max_holding_bars"]').fill("2")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-strategy").select_option(index=1)
    page.locator('#lab-version option:not([value=""])').first.wait_for(state="attached")
    assert page.locator("#lab-version").input_value()
    assert page.locator('#lab-strategy-form [name="max_holding_bars"]').input_value() == "2"
    page.locator("#lab-run").click()
    try:
        page.get_by_text("Prueba guardada", exact=False).wait_for(timeout=10000)
    except PlaywrightTimeoutError:
        raise AssertionError("LAB no guardó prueba: " + page.locator("#lab-notice").inner_text()) from None
    page.locator("#lab-chart canvas").first.wait_for()
    expect(page.locator("#lab-report tbody tr").first).to_contain_text("Límite de velas")
    run_id = page.locator("#lab-run-select").input_value()
    run = page.request.get("http://127.0.0.1:8799/lab/runs/" + run_id).json()
    assert run["engine_version"] == "lab-1.16"
    assert run["trades"][0]["reason"] == "TIME_LIMIT"
    page.screenshot(path="/tmp/orbis-lab-time-limit.png", full_page=True)

    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run-select").select_option(run_id)
    assert page.locator('#lab-strategy-form [name="max_holding_bars"]').input_value() == "2"
    expect(page.locator("#lab-report tbody tr").first).to_contain_text("Límite de velas")
    assert page.request.get("http://127.0.0.1:8799/lab/runs/" + run_id + "/verify").json()["verified"]
    assert not errors, errors
    browser.close()
    print("LAB time limit: editor, execution, restart and replay OK")
