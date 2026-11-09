"""CSV → versión → barrido desarrollo → ganador en reserva → recarga."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


START = 1_735_689_600
BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
OUTPUT = Path(os.environ["ORBIS_LAB_PATH"]).resolve().parent.parent
csv = "time,open,high,low,close,volume\n" + "\n".join(
    f"{START + i * 3600},{100+i},{102+i},{99+i},{101+i},10"
    for i in range(200)
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    created = page.request.post(BASE + "/lab/create", data={"passphrase": "browser isolated LAB reserve password"})
    assert created.ok, created.text()
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-csv [name="file"]').set_input_files(
        {"name": "grid.csv", "mimeType": "text/csv", "buffer": csv.encode()})
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-strategy-form [name="name"]').fill("EMA para barrido")
    page.locator('#lab-strategy-form [name="period"]').first.fill("10")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    expect(page.locator("#lab-dataset")).not_to_have_value("")
    expect(page.locator("#lab-version")).not_to_have_value("")
    page.locator("#lab-opt-stops").fill("0.02 0.04")
    page.locator("#lab-opt-targets").fill("0.05 0.10")
    page.locator("#lab-optimize").click()
    page.get_by_text("Exploración guardada", exact=False).wait_for()
    expect(page.locator("#lab-opt-report")).to_contain_text("Reserva · sólo elegido")
    expect(page.locator("#lab-opt-report .lab-opt-sensitivity")).to_contain_text("Parámetros vecinos")
    expect(page.locator("#lab-opt-report")).to_contain_text("muestra inestable")
    assert page.locator("#lab-opt-report .lab-opt-table tbody tr").count() == 4
    assert page.locator("#lab-opt-report .lab-opt-winner").count() == 1
    run_id = page.locator("#lab-opt-select").input_value()
    saved = page.request.get(BASE + "/lab/optimizations/" + run_id).json()
    assert saved["split_index"] == 140 and saved["test_bars"] == 60
    assert len(saved["candidates"]) == 4 and saved["out_of_sample"] is not None
    assert saved["sensitivity"]["neighbor_count"] == 2
    assert not any("out_of_sample" in candidate for candidate in saved["candidates"])
    heatmap = page.locator("#lab-opt-report .lab-parameter-map")
    expect(heatmap.get_by_role("table", name="Mapa de calor · Neto de desarrollo")).to_be_visible()
    expect(heatmap.locator("thead th")).to_have_text(["Stop ↓ / Objetivo →", "5 %", "10 %"])
    expect(heatmap.locator("tbody th")).to_have_text(["2 %", "4 %"])
    assert heatmap.locator("td").evaluate_all("cells => cells.map(cell => cell.title)") == [
        "Neto exacto · " + candidate["train_metrics"]["net_profit"] for candidate in saved["candidates"]
    ]
    expect(heatmap.locator("[data-selected]")).to_contain_text("Elegida")
    cells = page.evaluate("""() => {
      const host = document.createElement('div');
      OrbisLabParameterMap.render(host, [
        {stop:'1', target:'1', net:'-40', eligible:true},
        {stop:'1', target:'2', net:'40', eligible:true, selected:true},
        {stop:'2', target:'1', net:'90000', eligible:false},
        {stop:'2', target:'2', net:'0', eligible:true},
        {stop:'3', target:'1', net:'-1E-10', eligible:true},
      ]);
      return Array.from(host.querySelectorAll('td[data-tone]'), cell => [
        cell.dataset.tone, cell.style.getPropertyValue('--heat'), cell.textContent, cell.title
      ]);
    }""")
    assert cells[:4] == [
        ["loss", "38%", "−40,00", "Neto exacto · -40"],
        ["gain", "38%", "+40,00Elegida", "Neto exacto · 40"],
        ["excluded", "", "+90.000,00Excluida", "Neto exacto · 90000"],
        ["zero", "", "0,00", "Neto exacto · 0"],
    ]
    tone, shade, visible, exact = cells[4]
    assert (tone, visible, exact) == ("loss", "− <0,01", "Neto exacto · -1E-10")
    assert 10 <= float(shade.rstrip("%")) < 11
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-opt-select").select_option(run_id)
    expect(page.locator("#lab-opt-report")).to_contain_text("Reserva · sólo elegido")
    expect(page.locator("#lab-opt-report .lab-opt-sensitivity")).to_contain_text("Parámetros vecinos")
    expect(heatmap.locator("td")).to_have_count(4)
    heatmap.screenshot(path=str(OUTPUT / "optimization-map-desktop.png"))
    page.set_viewport_size({"width":390, "height":844})
    expect(heatmap).to_be_visible()
    assert heatmap.evaluate("node => node.getBoundingClientRect().right <= innerWidth")
    assert heatmap.locator(".lab-parameter-map-scroll").evaluate("node => node.scrollWidth <= node.clientWidth")
    heatmap.screenshot(path=str(OUTPUT / "optimization-map-mobile.png"))
    page.route("**/lab/optimizations/" + run_id + "/verify", lambda route: route.fulfill(
        status=200, json={"status":"mismatch", "reason":"Prueba aislada de la vista"}))
    page.locator("#lab-opt-report .lab-verify button").click()
    expect(heatmap).to_be_hidden()
    assert not errors, errors
    browser.close()
    print("LAB grid: symmetric development map, exclusions, exact values, mobile and saved reopen")
