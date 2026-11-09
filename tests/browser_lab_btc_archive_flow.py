"""BTC 2012 real: descarga → dataset local → estrategia → backtest → recarga."""

import os

from playwright.sync_api import expect, sync_playwright


base_url = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base_url + "/ui/#/config")
    page.wait_for_load_state("networkidle")
    page.locator("#cfg-privacidad").get_by_text("LAB activo", exact=True).click()
    create = page.locator("#cfg-lab-create-form")
    create.locator("input[name=passphrase]").fill("orbis isolated btc archive browser password")
    create.get_by_role("button", name="Crear LAB cifrado").click()
    expect(page.locator("#cfg-lab-status")).to_contain_text("desbloqueado")
    page.goto(base_url + "/ui/#/laboratorio")
    page.locator("#lab-btc-archive").click()
    page.get_by_text("Histórico BTC/USD conservado:", exact=False).wait_for(timeout=30000)
    dataset_id = page.locator("#lab-dataset").input_value()
    dataset = page.request.get(base_url + "/lab/datasets/" + dataset_id).json()
    assert dataset["count"] > 5000 and dataset["start_t"] == 1325376000
    assert dataset["source"] == "bitstamp-btcusd-daily-public"
    assert dataset["verified_public"] and not dataset["gaps"]
    page.locator('#lab-strategy-form [name="name"]').fill("RSI BTC histórico")
    page.locator('#lab-rule-list [name="kind"]').first.select_option("RSI_LT")
    page.locator('#lab-rule-list [name="period"]').first.fill("14")
    page.locator('#lab-rule-list [name="value"]').first.fill("10")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for(timeout=10000)
    page.locator("#lab-strategy").select_option(index=1)
    expect(page.locator("#lab-version")).not_to_have_value("", timeout=10000)
    page.locator("#lab-run").click()
    try:
        page.get_by_text("Prueba guardada", exact=False).wait_for(timeout=30000)
    except Exception:
        raise AssertionError(page.locator("#lab-notice").inner_text()) from None
    expect(page.locator("#lab-run-select")).not_to_have_value("", timeout=30000)
    run_id = page.locator("#lab-run-select").input_value()
    run = page.request.get(base_url + "/lab/runs/" + run_id).json()
    assert run["dataset_id"] == dataset_id
    assert run["dataset_hash"] == dataset["hash"]
    assert run["engine_version"] == "lab-1.16"
    page.locator("#lab-chart canvas").first.wait_for(timeout=10000)
    assert page.locator("#lab-chart").bounding_box()["height"] >= 480
    page.locator("#lab-equity-range select").select_option("10")
    assert page.locator("#lab-equity-range").get_attribute("data-view") == "10"
    assert dataset["hash"][:16] in page.locator("#lab-report").inner_text()
    history = page.locator("#lab-run-history")
    assert history.get_attribute("data-view") == "1"
    history.locator("select").select_option("5")
    assert history.get_attribute("data-view") == "5"
    history.locator("select").select_option("10")
    assert history.get_attribute("data-view") == "10"
    history.locator("select").select_option("custom")
    history.locator("[data-range-from]").fill("2012-01-01")
    history.locator("[data-range-to]").fill("2012-12-31")
    history.locator("[data-range-apply]").click()
    assert history.get_attribute("data-view") == "custom"
    assert "2012-01-01" in history.locator("[data-range-coverage]").inner_text()
    history.locator("select").select_option("all")
    assert history.get_attribute("data-view") == "all"
    page.locator("#lab-research-form button").click()
    page.get_by_text("Investigación guardada", exact=False).wait_for(timeout=30000)
    page.locator("#lab-research-chart canvas").first.wait_for(timeout=10000)
    assert page.locator("#lab-research-chart").bounding_box()["height"] >= 440
    research_history = page.locator("#lab-research-history")
    assert research_history.get_attribute("data-view") == "1"
    research_history.locator("select").select_option("custom")
    research_history.locator("[data-range-from]").fill("2012-01-01")
    research_history.locator("[data-range-to]").fill("2012-12-31")
    research_history.locator("[data-range-apply]").click()
    assert research_history.get_attribute("data-view") == "custom"
    assert page.locator("#lab-research-event-rows tr").count() > 0
    page.reload()
    page.locator("#lab-dataset option[value='" + dataset_id + "']").wait_for(state="attached")
    expect(page.locator("#lab-research-history select")).to_have_value("custom", timeout=10000)
    expect(page.locator("#lab-research-history [data-range-coverage]")).to_contain_text("2012-01-01")
    assert page.request.get(base_url + "/lab/runs/" + run_id).json()["dataset_hash"] == dataset["hash"]
    page.route("**/lab/datasets/" + dataset_id + "?view=chart",
               lambda route: route.fulfill(json={**dataset, "hash": "0" * 64}))
    page.locator("#lab-run-select").select_option(run_id)
    page.get_by_text("La huella del dataset no coincide", exact=False).wait_for(timeout=10000)
    assert "gráfico bloqueado" in page.locator("#lab-chart").inner_text()
    expect(page.locator("#lab-run-history")).to_be_hidden()
    expect(page.locator("#lab-equity-range")).to_be_hidden()
    assert not errors, errors
    print("LAB BTC archive flow passed: 2012 data, versioned backtest, persisted hash")
    browser.close()
