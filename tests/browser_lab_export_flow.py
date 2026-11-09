"""CSV → backtest → descarga local de evidencia LAB verificada."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
OUTPUT = os.environ.get("ORBIS_TEST_EXECUTION_DIRECTORY")
START = 1735689600
CSV = "time,open,high,low,close,volume\n" + "\n".join(
    f"{START + i * 3600},{10+i},{11+i},{9+i},{10+i},10" for i in range(8)
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("LAB activo", exact=True).click()
    create = page.locator("#cfg-lab-create-form")
    create.locator("input[name=passphrase]").fill("orbis lab isolated export password")
    create.get_by_role("button", name="Crear LAB cifrado").click()
    page.locator("#cfg-lab-status").get_by_text("desbloqueado", exact=False).wait_for(timeout=30000)
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-csv [name="file"]').set_input_files({
        "name": "velas.csv", "mimeType": "text/csv", "buffer": CSV.encode(),
    })
    page.locator('#lab-csv button[type="submit"]').click()
    page.get_by_text("CSV conservado", exact=False).wait_for()
    page.locator('#lab-strategy-form [name="name"]').fill("EMA exportable")
    page.locator('#lab-strategy-form [name="period"]').first.fill("2")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run").click()
    page.locator("#lab-notice").get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-export-action").wait_for()
    with page.expect_download() as outgoing:
        page.locator("#lab-export-action").click()
    download = outgoing.value
    assert download.suggested_filename.startswith("orbis-lab-")
    bundle = json.loads(Path(download.path()).read_text())
    assert bundle["schema"] == "orbis.lab.evidence.v2"
    assert bundle["environment"] == "LAB"
    assert bundle["verification"]["verified"] is True
    assert bundle["signals"]
    assert bundle["candles"]
    page.locator("#lab-export-format").select_option("v3")
    with page.expect_download() as split_outgoing:
        page.locator("#lab-export-action").click()
    split_file = split_outgoing.value
    assert "-70-30" in split_file.suggested_filename
    split_bundle = json.loads(Path(split_file.path()).read_text())
    assert split_bundle["schema"] == "orbis.lab.evidence.v3"
    assert split_bundle["split"]["method"] == "chronological_70_30_purged_7d_v1"
    assert all("partition" in signal for signal in split_bundle["signals"])
    with page.expect_download() as strategy_outgoing:
        page.locator("#lab-export-strategy").click()
    strategy_file = strategy_outgoing.value
    assert strategy_file.suggested_filename.startswith("orbis-estrategia-")
    strategy_bundle = json.loads(Path(strategy_file.path()).read_text())
    assert strategy_bundle["schema"] == "orbis.lab.strategy.v1"
    assert strategy_bundle["environment"] == "LAB"
    page.locator("#lab-import-strategy-file").set_input_files({
        "name": "estrategia.json", "mimeType": "application/json",
        "buffer": json.dumps(strategy_bundle).encode(),
    })
    page.get_by_text("Estrategia LAB importada", exact=False).wait_for()
    assert len(page.request.get(BASE + "/lab/strategies").json()) == 2
    assert page.locator('#lab-strategy-form [name="name"]').input_value().endswith("importada")

    research_candles = [
        {"t": START + index * 3600, "o": str(1000 - index),
         "h": str(1001 - index), "l": str(998 - index),
         "c": str(999 - index), "v": str(10 + index)}
        for index in range(540)
    ]
    dataset = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "browser-research-export-fixture",
        "candles": research_candles,
    })
    assert dataset.ok, dataset.text()
    research = page.request.post(BASE + "/lab/research/runs", data={
        "dataset_id": dataset.json()["id"],
        "rule": {"op": "AND", "children": [
            {"kind": "BEARISH_STREAK", "period": 3},
            {"kind": "VOLUME_RISING_STREAK", "period": 3},
        ]},
    })
    assert research.ok, research.text()
    second = page.request.post(BASE + "/lab/research/runs",data={
        "dataset_id":dataset.json()["id"],"rule":{"kind":"BEARISH_STREAK","period":4}})
    assert second.ok,second.text()
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-research-select").select_option(research.json()["id"])
    page.locator("#lab-research-export-format").select_option("v3")
    with page.expect_download() as research_outgoing:
        page.locator("#lab-research-export").click()
    research_bundle = json.loads(Path(research_outgoing.value.path()).read_text())
    assert research_bundle["schema"] == "orbis.lab.research.evidence.v3"
    assert research_bundle["cases"]
    assert "validation_nonoverlap_indices" in research_bundle["split"]
    panel = page.locator("#lab-cohort")
    panel.locator("summary").click()
    button = panel.get_by_role("button",name="Exportar conjunto")
    expect(button).to_be_disabled()
    for identifier in (research.json()["id"],second.json()["id"]):
        panel.locator(f'input[value="{identifier}"]').check()
    expect(button).to_be_disabled()
    panel.locator('input[name="boundary"]').fill("2025-01-11")
    expect(button).to_be_enabled()
    with page.expect_download() as combined:
        button.click()
    output = json.loads(Path(combined.value.path()).read_text())
    assert output["schema"] == "orbis.lab.research.cohort.v1"
    assert output["counts"]["purged_boundary"] > 0
    assert len(output["sources"]) == 2 and output["development"] and output["validation"]
    expect(panel.get_by_role("status")).to_contain_text("reserva")
    if OUTPUT:
        panel.screenshot(path=str(Path(OUTPUT)/"lab-cohort-desktop.png"))
        page.set_viewport_size({"width":390,"height":844})
        panel.screenshot(path=str(Path(OUTPUT)/"lab-cohort-mobile.png"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
    assert not errors, errors
    browser.close()
    print("LAB: exportación local verificada desde la interfaz")
