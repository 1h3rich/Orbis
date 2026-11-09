"""API y UI nuevas juntas, en servidor LAB aislado con histórico ficticio."""

import re
import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8798")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors, marker_calls = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: marker_calls.append(response.url)
            if "/lab/runs/" in response.url and "/markers?" in response.url else None)
    if os.environ.get("ORBIS_TEST_LAB_WINDOW"):
        assert page.request.post(BASE+"/lab/unlock",data={"passphrase":"Fixture LAB range 22"}).ok
    runs = page.request.get(BASE + "/lab/runs")
    assert runs.ok and len(runs.json()) == 1
    run_id = runs.json()[0]["id"]
    shell_response = page.request.get(BASE + "/lab/runs/" + run_id + "?view=chart&markers=window")
    assert shell_response.ok and shell_response.headers["cache-control"] == "no-store"
    shell = shell_response.json()
    assert shell["markers_windowed"] is True
    assert shell["events"] == shell["trades"] == []
    assert shell["signal_count"] > 1_000
    assert shell["trade_count"] > 1_000

    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(run_id)
    chart = page.locator("#lab-chart")
    expect(chart).to_have_attribute("data-signal-count", str(shell["signal_count"]))
    expect(chart).to_have_attribute("data-marker-visible-count", re.compile(r"^[1-9][0-9]*$"))
    initial = int(chart.get_attribute("data-marker-visible-count"))
    total = shell["signal_count"] + 2 * shell["trade_count"]
    assert 0 < initial < total
    page.locator("#lab-run-history select").select_option("all")
    expect(chart).to_have_attribute("data-marker-visible-count", str(total))
    page.locator("#lab-run-history select").select_option("1")
    expect(chart).not_to_have_attribute("data-marker-visible-count", str(total))
    assert len(marker_calls) >= 2
    assert page.locator("#lab-trade-rows tr").count() > 0
    assert page.locator("#lab-event-rows tr").count() == 50
    assert not errors, errors
    browser.close()
