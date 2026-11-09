"""Grupos Y/O anidados: editor visual, versión inmutable y marcador."""

from datetime import datetime, timezone

from playwright.sync_api import sync_playwright


start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
candles = [
    {"t": start + i * 3600, "o": str(10 + i), "h": str(11 + i),
     "l": str(9 + i), "c": str(10 + i), "v": "10"}
    for i in range(8)
]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    imported = page.request.post("http://127.0.0.1:8799/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "browser-nested-fixture", "candles": candles,
    })
    assert imported.ok, imported.text()
    page.goto("http://127.0.0.1:8799/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator('#lab-strategy-form [name="name"]').fill("EMA y grupo alternativo")
    page.locator('#lab-rule-list > .lab-rule [name="period"]').fill("2")
    page.locator("#lab-add-group").click()
    group = page.locator("#lab-rule-list > .lab-subgroup")
    group.locator('.lab-group-head [name="group_op"]').select_option("OR")
    first = group.locator(".lab-group-children > .lab-rule").first
    first.locator('[name="kind"]').select_option("RSI_GT")
    first.locator('[name="period"]').fill("2")
    first.locator('[name="value"]').fill("70")
    group.locator(".lab-add-nested-rule").click()
    second = group.locator(".lab-group-children > .lab-rule").nth(1)
    second.locator('[name="kind"]').select_option("VOLUME_ABOVE_AVG")
    second.locator('[name="period"]').fill("2")
    second.locator('[name="value"]').fill("2")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    strategies = page.request.get("http://127.0.0.1:8799/lab/strategies").json()
    assert len(strategies) == 1
    first_version = strategies[0]["version_id"]
    entry = strategies[0]["definition"]["entry"]
    assert entry["op"] == "AND" and len(entry["children"]) == 2
    assert entry["children"][1]["op"] == "OR"
    assert [part["kind"] for part in entry["children"][1]["children"]] == ["RSI_GT", "VOLUME_ABOVE_AVG"]
    page.locator("#lab-strategy").select_option(index=1)
    page.locator("#lab-run").click()
    page.get_by_text("Prueba guardada", exact=False).wait_for()
    page.locator("#lab-chart canvas").first.wait_for()
    assert int(page.locator("#lab-chart").get_attribute("data-signal-count")) > 0
    page.screenshot(path="/tmp/orbis-lab-nested.png", full_page=True)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-strategy").select_option(index=1)
    group = page.locator("#lab-rule-list > .lab-subgroup")
    assert group.locator('.lab-group-head [name="group_op"]').input_value() == "OR"
    assert group.locator(".lab-group-children > .lab-rule").count() == 2
    group.locator(".lab-group-children > .lab-rule").first.locator('[name="value"]').fill("80")
    page.locator("#lab-save-strategy").click()
    page.get_by_text("Estrategia guardada", exact=False).wait_for()
    versions = page.request.get(
        "http://127.0.0.1:8799/lab/strategies/" + strategies[0]["id"] + "/versions"
    ).json()
    assert [row["version"] for row in versions] == [1, 2]
    assert versions[0]["id"] == first_version
    assert versions[0]["definition"]["entry"]["children"][1]["children"][0]["threshold"] == "70"
    assert versions[1]["definition"]["entry"]["children"][1]["children"][0]["threshold"] == "80"
    group = page.locator("#lab-rule-list > .lab-subgroup")
    group.locator(".lab-group-head + .lab-group-children + .lab-group-actions > .lab-add-nested-group").click()
    level_two = group.locator(".lab-group-children > .lab-subgroup")
    level_two.locator(".lab-group-actions > .lab-add-nested-group").click()
    level_three = level_two.locator(".lab-group-children > .lab-subgroup")
    assert level_three.locator(".lab-group-actions > .lab-add-nested-group").is_disabled()
    assert not errors, errors
    print("Browser LAB nested flow passed: Y/O group, backtest, markers, immutable versions")
    browser.close()
