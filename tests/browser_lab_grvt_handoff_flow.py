"""LAB → GRVT PAPER selecciona la versión antigua sin crear sesiones."""

import os
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from app.lab.engine import validate_candles
from app.lab.store import LabStore


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8765")
START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
definition = {"direction": "SHORT", "initial_capital": "1000", "allocation": "1",
              "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
              "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}
raw = [{"t": START + i * 3600, "o": str(100 + i), "h": str(101 + i),
        "l": str(99 + i), "c": str(100 + i), "v": "10"} for i in range(8)]
store = LabStore(Path(os.environ["ORBIS_LAB_PATH"]))
bars = validate_candles(raw, "1h", now=START + 20 * 3600)[0]
dataset = store.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public", bars, [], verified_public=True)
original = store.create_strategy("GRVT versión antigua", definition)
store.add_version(original["id"], dict(definition, direction="LONG"))

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    encrypted = page.request.post(BASE + "/paper/perp/create", data={
        "passphrase": "owned lab grvt handoff password",
    })
    assert encrypted.status == 200, encrypted.text()
    account = page.request.post(BASE + "/paper/perp/accounts", data={
        "name": "Prueba de entrega", "initial_balance": "10000",
        "max_position_notional": "1000", "max_total_notional": "2000",
        "max_leverage": "2", "max_positions": 2,
        "fee_rate": "0.0005", "slippage_bps": "5",
    })
    assert account.status == 201, account.text()
    run = page.request.post(BASE + "/lab/runs", data={
        "dataset_id": dataset["id"], "version_id": original["version_id"],
    })
    assert run.ok, run.text()

    page.goto(BASE + "/ui/#/laboratorio")
    page.locator("#lab-run-select").select_option(run.json()["id"])
    page.get_by_role("button", name="Verificar prueba").click()
    expect(page.locator("#lab-run-verify")).to_have_attribute("data-state", "verified")
    page.get_by_role("button", name="Seguir en GRVT PAPER").click()
    page.wait_for_url("**/#/perpetuos?**")
    form = page.locator("#perp-signal-form")
    expect(form.locator('[name="version_id"]')).to_have_value(original["version_id"])
    expect(form.locator('[name="dataset_id"]')).to_have_value(dataset["id"])
    expect(form.locator('[name="execution_mode"]')).to_have_value("OBSERVE")
    expect(page.locator("#perp-signal-sessions")).to_contain_text("Sin sesiones")
    expect(page.locator("#perp-signal-readiness")).to_contain_text("Histórico no listo")
    expect(page.locator("#perp-signal-create")).to_be_disabled()
    assert not errors, errors
    print("LAB → GRVT PAPER: versión antigua conservada; sin activación automática")
    browser.close()
