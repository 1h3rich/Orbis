"""GRVT PAPER conserva la versión y exige elegir iniciar tras descargar."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8772"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/market/candles?*", lambda route: route.fulfill(status=200, json={
        "symbol": "BTC/USDT:PERP", "resolved_provider": "grvt", "timeframe": "1h",
        "available": True, "candles": []}))
    initial = page.request.get(BASE + "/lab/datasets").json()
    assert len(initial) == 1
    old_id = initial[0]["id"]
    page.goto(BASE + "/ui/#/perpetuos")
    form = page.locator("#perp-signal-form")
    expect(page.locator("#perp-signal-readiness")).to_contain_text("Histórico no listo")
    expect(page.locator("#perp-signal-create")).to_be_disabled()
    expect(page.locator("#perp-signal-freshen")).to_be_visible()
    version_id = form.locator('[name="version_id"]').input_value()
    page.locator("#perp-signal-freshen").click()
    expect(page.locator("#perp-signal-readiness")).to_contain_text("Histórico al día")
    expect(page.locator("#perp-signal-lineage")).to_contain_text("Origen LAB")
    expect(page.locator("#perp-signal-create")).to_be_enabled()
    assert form.locator('[name="dataset_id"]').input_value() != old_id
    expect(form.locator('[name="version_id"]')).to_have_value(version_id)
    expect(form.locator('[name="execution_mode"]')).to_have_value("OBSERVE")
    expect(page.locator("#perp-signal-sessions")).to_contain_text("Sin sesiones")
    saved = page.request.get(BASE + "/lab/datasets").json()
    assert len(saved) == 2 and next(row for row in saved if row["id"] == old_id) == initial[0]
    page.reload()
    expect(page.locator("#perp-signal-lineage")).to_contain_text("Origen LAB")
    assert not errors, errors
    print("GRVT PAPER: histórico reciente nuevo, versión conservada, sin sesión automática")
    browser.close()
