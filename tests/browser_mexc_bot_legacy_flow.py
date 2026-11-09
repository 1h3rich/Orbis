"""Read an old orphaned Spot order from the venue without touching trade state."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.test_mexc_execution_state import PASSWORD


base = os.environ["ORBIS_TEST_BASE_URL"]
canceled = os.environ.get("ORBIS_TEST_MEXC_LEGACY_CANCELED") == "1"
recover_fills = os.environ.get("ORBIS_TEST_MEXC_LEGACY_RECOVER_FILLS") == "1"
assert not (canceled and recover_fills)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    archive_prompts = []
    def confirm(dialog):
        if dialog.message.startswith(("MEXC confirma la cancelación", "Ejecuciones MEXC:")):
            archive_prompts.append(dialog.message)
            if len(archive_prompts) == 1:
                dialog.dismiss()
                return
        dialog.accept()
    page.on("dialog", confirm)
    page.goto(base + "/ui/#/real")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear", exact=True).click()
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button", name="Consultar MEXC", exact=True).click()
    page.locator("#real-mexc-trade-verify > summary").click()
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("fixture-trade")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button", name="Verificar Trade", exact=True).click()
    panel = page.locator("#real-mexc-bots")
    panel.locator("summary").click()
    expect(panel.locator('[name="paper"] option')).to_have_count(2)
    panel.locator('[name="paper"]').select_option(index=1)
    panel.locator('[name="budget"]').fill("20")
    panel.get_by_role("button", name="Activar bot", exact=True).click()
    expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")
    assert page.request.post(base + "/__test/fault/legacy").ok
    assert page.request.post(base + "/__test/entry").ok
    expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado", timeout=15000)
    panel.get_by_role("button", name="Diario", exact=True).click()
    journal = panel.locator("[data-bot-journal]")
    expect(journal).to_contain_text("Sin reserva local")
    lookup = journal.get_by_role("button", name="Consultar en MEXC", exact=True)
    notice = panel.locator("[data-bots-state]")
    with page.expect_response("**/mexc-review") as first:
        lookup.click()
    assert first.value.ok, first.value.text()
    missing = first.value.json()
    assert missing["state"] == "UNVERIFIED" and first.value.request.method == "GET"
    assert first.value.headers["cache-control"] == "no-store"
    expect(notice).to_contain_text("Orden sin verificar")
    assert page.request.get(first.value.url).status == 401
    assert page.request.get(first.value.url, headers={"Origin":"https://outside.invalid"}).status == 403
    assert page.request.get(base + "/__test/posts").json()["count"] == 0
    assert page.request.post(base + "/__test/legacy-found" + ("?canceled=true" if canceled else "")).ok

    with page.expect_response("**/mexc-review") as second:
        lookup.click()
    assert second.value.ok, second.value.text()
    found = second.value.json()
    assert found["state"] == "MATCHED" and found["client_order_id"] == missing["client_order_id"]

    def rearm_same_bot(fill_count):
        panel.get_by_role("button",name="Reanudar",exact=True).click()
        with page.expect_response("**/mexc-bots/*/rearm") as resumed:
            panel.get_by_role("button",name="Revisar y reanudar",exact=True).click()
        assert resumed.value.ok, resumed.value.text()
        assert resumed.value.json()["position"]["fill_count"] == fill_count
        expect(panel.locator("[data-bots-badge]")).to_contain_text("activo")

    if canceled:
        assert found["external_status"] == "CANCELED" and found["fill_count"] == 0
        assert len(found["resolution_digest"]) == 64 and len(archive_prompts) == 1
        expect(notice).to_contain_text("pendiente de archivar")
        expect(journal).to_contain_text("Sin reserva local")
        headers = second.value.request.headers
        body = {"confirmation":"ARCHIVAR_CANCELACION_SIN_FILLS","resolution_digest":"0"*64}
        assert page.request.post(second.value.url+"/confirm",headers=headers,data=body).status == 409
        assert page.request.post(second.value.url+"/confirm",data=body).status == 401
        assert page.request.post(second.value.url+"/confirm",headers={**headers,"Origin":"https://outside.invalid"},data=body).status == 403
        assert page.request.post(base + "/__test/legacy-status/NEW").ok
        body["resolution_digest"] = found["resolution_digest"]
        assert page.request.post(second.value.url+"/confirm",headers=headers,data=body).status == 409
        expect(journal).to_contain_text("Sin reserva local")
        assert page.request.post(base + "/__test/legacy-status/CANCELED").ok
        with page.expect_response("**/mexc-review/confirm") as saved:
            lookup.click()
        assert saved.value.ok, saved.value.text()
        assert len(archive_prompts) == 2 and not saved.value.json()["replayed"]
        expect(journal).to_contain_text("Cancelación archivada")
        expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
        replay = page.request.post(saved.value.url,headers=saved.value.request.headers,data=saved.value.request.post_data_json)
        assert replay.ok and replay.json()["replayed"], replay.text()
        rearm_same_bot(0)
        panel.get_by_role("button",name="Detener bot",exact=True).click()
        expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
        page.get_by_role("button",name="Bloquear",exact=True).click()
        page.locator("#real-passphrase").fill(PASSWORD)
        page.get_by_role("button",name="Desbloquear",exact=True).click()
        panel.locator("summary").click()
        panel.get_by_role("button",name="Diario",exact=True).click()
        expect(journal).to_contain_text("Cancelación archivada")
    elif recover_fills:
        assert found["external_status"] == "FILLED" and found["fill_count"] == 1
        assert len(found["fill_recovery_digest"]) == 64 and found["fees_by_asset"] == [["USDT","0.02"]]
        expect(notice).to_contain_text("conciliación pendiente")
        expect(journal).to_contain_text("Sin reserva local")
        expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(0)
        with page.expect_response("**/mexc-review/confirm") as saved:
            lookup.click()
        assert saved.value.ok, saved.value.text()
        assert len(archive_prompts) == 2 and saved.value.json()["state"] == "LEGACY_FILLS_RECONCILED"
        expect(journal).to_contain_text("Orden conciliada")
        expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
        expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(1)
        replay = page.request.post(saved.value.url,headers=saved.value.request.headers,data=saved.value.request.post_data_json)
        assert replay.ok and replay.json()["replayed"], replay.text()
        assert page.request.get(base + "/__test/posts").json()["count"] == 1
        rearm_same_bot(1)
        assert page.request.get(base + "/__test/posts").json()["count"] == 1
        assert page.request.post(base + "/__test/target").ok
        expect(panel.locator("[data-bots-list]")).to_contain_text("Fills: 2",timeout=15000)
        expect(page.locator("#real-history").get_by_text("SELL", exact=True)).to_have_count(1)
        panel.get_by_role("button",name="Detener bot",exact=True).click()
        expect(panel.locator("[data-bots-list]")).to_contain_text("Pausado")
    else:
        assert found["external_status"] == "FILLED" and found["fill_count"] == 1 and "resolution_digest" not in found
        expect(notice).to_contain_text("conciliación pendiente")
        expect(journal).to_contain_text("Sin reserva local")
        expect(panel.locator("[data-bots-list]")).to_contain_text("Posición pendiente de verificación")
    expect(page.locator("#real-history").get_by_text("BUY", exact=True)).to_have_count(int(recover_fills))
    assert page.request.get(base + "/__test/posts").json()["count"] == (2 if recover_fills else 0 if canceled else 1)
    assert not errors, errors
    directory = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])
    (directory / "result.json").write_text(json.dumps({"absence_unverified":True, "matched_with_fills":not canceled,
        "anonymous_rejected":True, "cross_origin_rejected":True, "orders_from_lookup":0,
        "local_reservation_recreated":False, "bookings_created":int(recover_fills), "page_errors":errors,
        **({"archived_without_fills":True, "first_confirmation_declined":True, "reopened":True,
            "explicit_rearm":True, "replay_idempotent":True, "changed_venue_rejected":True} if canceled else {}),
        **({"recovered_original":True, "first_confirmation_declined":True, "explicit_rearm":True,
            "replay_idempotent":True, "next_sale_settled":True} if recover_fills else {})}))
    browser.close()
