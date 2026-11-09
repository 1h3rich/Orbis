"""CSV → versión → walk-forward → inspección → verificación → recarga."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


START = 1_735_689_600
BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
OUTPUT = Path(os.environ["ORBIS_LAB_PATH"]).resolve().parent.parent
csv = "time,open,high,low,close,volume\n" + "\n".join(
    f"{START + i * 3600},{100+i},{102+i},{99+i},{101+i},10"
    for i in range(600)
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    assert page.request.get(BASE + "/lab/status").json()["format"] == "missing", "Usa una base LAB temporal vacía"
    created = page.request.post(BASE + "/lab/create", data={"passphrase": "browser isolated LAB reserve password"})
    assert created.ok, created.text()
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-csv [name="file"]').set_input_files(
        {"name": "walk-forward.csv", "mimeType": "text/csv", "buffer": csv.encode()})
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-strategy-form [name="name"]').fill("EMA para ventanas")
    page.locator('#lab-strategy-form [name="period"]').first.fill("10")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    expect(page.locator("#lab-dataset")).not_to_have_value("")
    expect(page.locator("#lab-version")).not_to_have_value("")
    page.locator("#lab-opt-method").select_option("walk-forward")
    assert page.locator(".lab-opt-split").is_hidden()
    assert page.locator("#lab-opt-fold-count").is_visible()
    assert page.locator("#lab-opt-final-fraction").is_visible()
    page.locator("#lab-opt-stops").fill("0.02 0.04")
    page.locator("#lab-opt-targets").fill("0.05 0.10")
    page.locator("#lab-optimize").click()
    page.get_by_text("Exploración guardada", exact=False).wait_for()
    report = page.locator("#lab-opt-report")
    expect(report).to_contain_text("reservas positivas")
    expect(report).to_contain_text("muestra inestable")
    expect(report).to_contain_text("Reserva final intacta")
    assert report.locator(".lab-opt-windows tbody tr").first.count() == 1
    assert report.locator(".lab-opt-windows > details").count() == 3
    report.locator(".lab-opt-windows > details").first.locator("summary").click()
    expect(report.locator(".lab-opt-windows > details").first).to_contain_text("Operaciones · desarrollo")
    run_id = page.locator("#lab-opt-select").input_value()
    saved = page.request.get(BASE + "/lab/optimizations/" + run_id).json()
    assert saved["method"] == "WALK_FORWARD_GRID_STOP_TARGET"
    assert len(saved["windows"]) == 3 and saved["eligible_folds"] == 3
    assert saved["final_holdout"]["bars"] == 120
    for selector, original in (("[data-map-window]", saved["windows"][0]), ("[data-map-final]", saved["final_holdout"])):
        heatmap = report.locator(selector).first
        expect(heatmap).to_be_visible()
        expect(heatmap.locator("[data-selected]")).to_have_count(1)
        assert heatmap.locator("td").evaluate_all("cells => cells.map(cell => cell.title)") == [
            "Neto exacto · " + candidate["train_metrics"]["net_profit"] for candidate in original["candidates"]
        ]
    report.locator(".lab-verify button").click()
    expect(report.locator(".lab-verify-badge")).to_have_text("REPRODUCIBLE")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-opt-select").select_option(run_id)
    expect(page.locator("#lab-opt-report")).to_contain_text("reservas positivas")
    assert page.locator("#lab-opt-method").input_value() == "walk-forward"
    expect(report.locator("[data-map-final] td")).to_have_count(4)
    report.locator("[data-map-final]").screenshot(path=str(OUTPUT / "walk-forward-final-map.png"))
    assert not errors, errors
    browser.close()
    print("LAB walk-forward: selección por ventana → reserva → verificación → persistencia OK")
