"""LAB verificado → Spot PAPER conserva una versión antigua y no la activa."""

import os

from playwright.sync_api import expect, sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8767")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    strategies = page.request.get(BASE + "/lab/strategies")
    datasets = page.request.get(BASE + "/lab/datasets")
    assert strategies.ok and datasets.ok
    original = strategies.json()[0]
    dataset = datasets.json()[0]
    old_version = original["version_id"]
    revised = dict(original["definition"], direction="SHORT")
    new_version = page.request.post(BASE + "/lab/strategies/" + original["id"] + "/versions",
                                    data={"definition": revised})
    assert new_version.ok, new_version.text()
    run = page.request.post(BASE + "/lab/runs", data={
        "dataset_id": dataset["id"], "version_id": old_version,
    })
    assert run.ok, run.text()

    page.goto(BASE + "/ui/#/laboratorio")
    page.locator("#lab-run-select").select_option(run.json()["id"])
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    page.get_by_role("button", name="Seguir en Spot PAPER").click()
    page.wait_for_url("**/#/spot-paper?**")
    expect(page.locator("#spot-paper-dataset")).to_have_value(dataset["id"])
    expect(page.locator("#spot-paper-strategy")).to_have_value(original["id"])
    expect(page.locator("#spot-paper-version")).to_have_value(old_version)
    expect(page.locator("#spot-paper-session")).to_have_value("")
    expect(page.locator("#spot-paper-readiness")).to_contain_text("Histórico al día")
    expect(page.locator("#spot-paper-start")).to_be_enabled()
    assert not errors, errors
    print("LAB → Spot PAPER: versión antigua conservada; sin activación automática")
    browser.close()
