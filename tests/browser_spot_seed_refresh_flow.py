"""Histórico antiguo → descarga reciente → listo, sin cambiar estrategia ni sesión."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8771"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    initial = page.request.get(BASE + "/lab/datasets").json()
    assert len(initial) == 1
    old_id = initial[0]["id"]
    page.goto(BASE + "/ui/#/spot-paper")
    expect(page.locator("#spot-paper-readiness")).to_contain_text("Histórico no listo")
    expect(page.locator("#spot-paper-start")).to_be_disabled()
    expect(page.locator("#spot-paper-freshen")).to_be_visible()
    strategy_id = page.locator("#spot-paper-strategy").input_value()
    version_id = page.locator("#spot-paper-version").input_value()
    page.locator("#spot-paper-freshen").click()
    expect(page.locator("#spot-paper-readiness")).to_contain_text("Histórico al día")
    expect(page.locator("#spot-paper-lineage")).to_contain_text("Origen LAB")
    expect(page.locator("#spot-paper-start")).to_be_enabled()
    assert page.locator("#spot-paper-dataset").input_value() != old_id
    expect(page.locator("#spot-paper-strategy")).to_have_value(strategy_id)
    expect(page.locator("#spot-paper-version")).to_have_value(version_id)
    expect(page.locator("#spot-paper-session")).to_have_value("")
    saved = page.request.get(BASE + "/lab/datasets").json()
    assert len(saved) == 2 and next(row for row in saved if row["id"] == old_id) == initial[0]
    page.reload()
    expect(page.locator("#spot-paper-lineage")).to_contain_text("Origen LAB")
    assert not errors, errors
    print("Spot PAPER: histórico reciente nuevo, versión conservada, sin sesión automática")
    browser.close()
