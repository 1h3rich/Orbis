"""Investigación sin estrategia: casos, gráfico, persistencia y borrador."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from playwright.sync_api import expect, sync_playwright


start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
prices = [(100, 99), (99, 98), (98, 97), (97, 96), (96, 97), (97, 96),
          (96, 95), (95, 94), (94, 95), (95, 96), (96, 97), (97, 98)]
candles = [
    {"t": start + i * 3600, "o": str(open_price),
     "h": str(max(open_price, close_price) + 1),
     "l": str(min(open_price, close_price) - 1),
     "c": str(close_price), "v": str(10 + i)}
    for i, (open_price, close_price) in enumerate(prices)
]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    imported = page.request.post("http://127.0.0.1:8799/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "browser-research-fixture",
        "candles": candles,
    })
    assert imported.ok, imported.text()

    page.goto("http://127.0.0.1:8799/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-dataset option").nth(1).wait_for(state="attached")
    page.locator("#lab-research-form button").click()
    page.get_by_text("Investigación guardada", exact=False).wait_for()
    page.locator("#lab-research-chart canvas").first.wait_for()
    assert page.locator("#lab-research-chart").get_attribute("data-signal-count") == "3"
    assert page.locator("#lab-research-event-rows tr").count() == 3
    page.locator("#lab-research-report .lab-verify button").click()
    expect(page.locator("#lab-research-report .lab-verify")).to_have_attribute("data-state", "verified")
    page.locator("#lab-regime-panel summary").click()
    page.locator('#lab-regime-form [name="lookback"]').fill("2")
    page.locator('#lab-regime-form [name="threshold"]').fill("0.1")
    page.locator('#lab-regime-form [name="horizon"]').select_option("3600")
    page.locator('#lab-regime-form [name="train_fraction"]').select_option("0.6")
    page.locator('#lab-regime-form button[type="submit"]').click()
    expect(page.locator("#lab-regime-result")).to_contain_text("Volatilidad por vela al cierre")
    expect(page.locator("#lab-regime-result tbody tr")).to_have_count(3)
    expect(page.locator("#lab-regime-result thead")).to_contain_text("Reserva · n / media")
    expect(page.locator("#lab-regime-result")).to_contain_text("Reserva exploratoria")
    with page.expect_download() as pending:
        page.locator("#lab-research-export").click()
    download = pending.value
    assert download.suggested_filename.startswith("orbis-casos-")
    bundle = json.loads(Path(download.path()).read_text())
    assert bundle["schema"] == "orbis.lab.research.evidence.v2"
    assert bundle["verification"]["verified"] is True
    assert len(bundle["cases"]) == 3
    assert bundle["cases"][0]["features_at_close"]["signal_time"] == candles[2]["t"]
    assert bundle["cases"][0]["future_labels"]["max_up_pct"] is None
    assert page.locator("#lab-research-page").inner_text() == "1–3 / 3"
    assert "—%" not in page.locator(".lab-research-distribution").inner_text()
    assert "sin solape" in page.locator(".lab-research-distribution thead").inner_text().lower()
    assert page.locator(".lab-research-distribution tbody tr").first.locator("td").nth(1).inner_text() == "3"
    assert page.locator(".lab-research-distribution tbody tr").nth(1).locator("td").nth(1).inner_text() == "2"
    runs = page.request.get("http://127.0.0.1:8799/lab/research/runs").json()
    assert len(runs) == 1 and runs[0]["summary"]["cases"] == 3
    assert page.request.get("http://127.0.0.1:8799/lab/strategies").json() == []
    assert page.request.get("http://127.0.0.1:8799/lab/runs").json() == []
    page.screenshot(path="/tmp/orbis-lab-research.png", full_page=True)

    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-research-chart canvas").first.wait_for()
    assert page.locator("#lab-research-chart").get_attribute("data-signal-count") == "3"
    assert page.locator("#lab-research-event-rows tr").count() == 3

    page.locator("#lab-research-to-strategy").click()
    rules = page.locator("#lab-rule-list > .lab-rule")
    assert rules.count() == 2
    assert [rules.nth(i).locator('[name="kind"]').input_value() for i in range(2)] == [
        "BEARISH_STREAK", "VOLUME_ASCENDING_BARS"]
    assert page.locator('#lab-strategy-form [name="name"]').input_value().startswith("Hipótesis")
    page.locator('#lab-research-form [name="source"]').select_option("editor")
    assert page.locator("#lab-research-period").is_hidden()
    page.locator("#lab-research-form button").click()
    expect(page.locator("#lab-research-select option")).to_have_count(3)
    assert len(page.request.get("http://127.0.0.1:8799/lab/research/runs").json()) == 2
    with sqlite3.connect(os.environ["ORBIS_LAB_PATH"]) as db:
        db.execute("UPDATE research_events SET event_json='{}' WHERE run_id=? AND index_no=0", (runs[0]["id"],))
    page.locator("#lab-research-select").select_option(runs[0]["id"])
    expect(page.locator("#lab-research-report")).to_have_attribute("data-run-id", runs[0]["id"])
    page.locator("#lab-research-report .lab-verify button").click()
    expect(page.locator("#lab-research-report .lab-verify")).to_have_attribute("data-state", "mismatch")
    expect(page.locator("#lab-research-chart")).to_contain_text("Gráfico bloqueado")
    assert page.locator("#lab-research-chart canvas").count() == 0
    assert page.locator("#lab-research-to-strategy").is_disabled()
    assert page.locator("#lab-research-export").is_disabled()
    assert page.locator('#lab-regime-form button[type="submit"]').is_disabled()

    gap_candles = [bar for index, bar in enumerate(candles) if index != 5]
    gap_dataset = page.request.post("http://127.0.0.1:8799/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "browser-research-gap-fixture",
        "candles": gap_candles,
    })
    assert gap_dataset.ok, gap_dataset.text()
    gap_run = page.request.post("http://127.0.0.1:8799/lab/research/runs", data={
        "dataset_id": gap_dataset.json()["id"],
        "rule": {"op": "AND", "children": [
            {"kind": "BEARISH_STREAK", "period": 3},
            {"kind": "VOLUME_RISING_STREAK", "period": 3},
        ]},
    })
    assert gap_run.ok, gap_run.text()
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-research-select").select_option(gap_run.json()["id"])
    expect(page.locator("#lab-research-report .amber").first).to_contain_text("1 vela ausente entre 2 tramos")
    page.locator("#lab-research-report .lab-verify button").click()
    expect(page.locator("#lab-research-report .lab-verify")).to_have_attribute("data-state", "verified")
    assert not errors, errors
    print("Browser LAB research flow passed: events, markers, replay, tamper alert, draft, gap coverage")
    browser.close()
