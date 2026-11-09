"""Captura antes/después del IOC en Chromium con un GRVT Testnet controlado."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.browser_network import route_once
from tests.grvt_bracket_ui_fixture import PASSWORD
from tests.test_grvt_testnet_signer import KEY, SIGNER


BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUTPUT = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1100})
    errors, captures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.on("request", lambda request: captures.append(request.url) if request.url.endswith("/market-sample") else None)
    source = page.request.get(BASE + "/__test/state").json()["source"]
    key = "fake-testnet-key-" + source["sub_account_id"]
    page.goto(BASE + "/ui/#/real")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button", name="Desbloquear", exact=True).click()
    panel = page.locator("#real-grvt-bracket")
    panel.locator(":scope > summary").click()
    expect(panel.locator('[name="source"] option').nth(1)).to_be_attached()
    panel.locator('[name="source"]').select_option(source["client_order_id"])
    for name, value in {"sub_account_id":source["sub_account_id"], "size":"0.5", "limit_price":"100",
                        "stop_loss":"95", "take_profit":"110", "api_key":key}.items():
        panel.locator(f'[name="{name}"]').fill(value)
    panel.get_by_role("button", name="Revisar entrada", exact=True).click()
    expect(panel.locator("[data-bracket-review]")).to_be_visible()
    for name, value in {"api_key":key, "dedicated_private_key":KEY.hex(), "expected_signer":SIGNER}.items():
        panel.locator(f'[name="{name}"]').fill(value)
    panel.get_by_role("button", name="Firmar entrada · TESTNET", exact=True).click()
    group = panel.locator("[data-bracket-group]").first
    expect(group).to_contain_text("Acuse recibido")
    group.locator("summary").click()
    group.get_by_role("button", name="Muestra de parciales").click()
    probe = panel.locator("[data-bracket-probe]")
    expect(probe).to_be_visible()

    def capture(expected_position, *, lose_response=False):
        saved = []
        if lose_response:
            def drop_after_save(route):
                response = route.fetch()
                assert response.ok,response.text()
                saved.append(response.json())
                route.abort()
            route_once(page,"**/market-sample",drop_after_save)
        probe.locator('[name="probe_key"]').fill(key)
        if lose_response:
            probe.get_by_role("button", name="Capturar").click()
            expect(probe.locator("[data-probe-archive-state]")).to_have_text("3 guardadas")
            assert len(saved) == 1
            options = probe.locator('[name="saved_sample"] option')
            probe.locator('[name="saved_sample"]').select_option(options.last.get_attribute("value"))
            with page.expect_response(lambda reply:"/market-samples/" in reply.url) as reopened:
                probe.get_by_role("button",name="Abrir muestra",exact=True).click()
            response = reopened.value
            assert response.json() == saved[0]
        else:
            with page.expect_response("**/market-sample") as result:
                probe.get_by_role("button", name="Capturar").click()
            response = result.value
        assert response.ok, response.text()
        assert response.headers["cache-control"] == "no-store"
        expect(probe.locator('[name="probe_key"]')).to_have_value("")
        expect(probe.locator("[data-probe-result]")).to_contain_text("Posición restante · " + expected_position)
        with page.expect_download() as event:
            probe.get_by_role("button", name="Descargar JSON").click()
        download = event.value
        assert download.suggested_filename.startswith("orbis-grvt-testnet-")
        data = Path(download.path()).read_text()
        assert all(secret not in data for secret in (key, KEY.hex(), SIGNER))
        sample = json.loads(data)
        assert sample == response.json()
        assert sample["network"] == "TESTNET" and sample["authorization"] is False
        assert sample["orders"][0]["client_order_id"] == source["client_order_id"]
        assert len(sample["observed_at_ns"]) == 19
        return sample

    before = capture("0.5")
    assert before["ioc_terminal"] is None and before["planned_exit_sizes"] == {"STOP_LOSS":"0.5","TAKE_PROFIT":"0.5"}
    ioc = page.request.post(BASE + "/__test/market-reduction").json()["order_id"]
    probe.locator('[name="probe_ioc"]').fill(ioc)
    after = capture("0.4")
    assert after["ioc_terminal"] is True and after["orders"][3]["order_id"] == ioc
    assert after["stop_order_size"] == "0.5" and after["stop_book_size"] == "0.4"
    assert [fill["role"] for fill in after["fills"]] == ["ENTRY", "REDUCTION"]
    comparison = probe.locator("[data-probe-comparison]")
    comparison.locator("summary").click()
    expect(comparison).to_be_visible()

    def compare(first, second, status=200, final=None):
        comparison.locator('[name="probe_source"]').select_option("files")
        inputs = [("sample_before", first), ("sample_after", second)]
        if final is None:
            comparison.locator('[name="sample_final"]').set_input_files([])
        else:
            inputs.append(("sample_final", final))
        for name, payload in inputs:
            comparison.locator(f'[name="{name}"]').set_input_files({
                "name":name+".json","mimeType":"application/json","buffer":json.dumps(payload).encode(),
            })
        with page.expect_response("**/market-comparison") as event:
            comparison.get_by_role("button",name="Comparar",exact=True).click()
        response = event.value
        assert response.status == status, response.text()
        assert response.headers["cache-control"] == "no-store"
        return response.json()

    first_report = compare(before,after)
    position_row = comparison.get_by_role("row",name="Posición restante")
    expect(position_row.get_by_role("cell")).to_have_text(["0.5","0.4"])
    expect(comparison).to_contain_text("Diferencia 0 USDT")
    assert first_report["after"]["stop_book_size"] == "0.4"
    assert first_report["cash_delta_usdt"] == first_report["fill_delta_usdt"] == "-0.01"
    assert first_report["authorization"] is False and first_report["new_fills"] == 1

    compare(after,before,status=409)
    expect(comparison).to_contain_text("Muestras incompatibles")
    expect(comparison.locator("table")).to_have_count(0)
    assert page.request.post(BASE + "/__test/market-native-stop").ok
    stopped = capture("0",lose_response=True)
    final_report = compare(after,stopped)
    expect(position_row.get_by_role("cell")).to_have_text(["0.4","0"])
    assert final_report["after"]["stop_filled"] == "0.4"
    assert final_report["after"]["stop_reason"] == "REDUCE_ONLY_LIMIT"
    assert final_report["new_fills"] == 1 and final_report["unexplained_cash_delta_usdt"] == "0"

    timeline = compare(before, after, final=stopped)
    expect(position_row.get_by_role("cell")).to_have_text(["0.5", "0.4", "0"])
    expect(comparison).to_contain_text("Orden de fills · IOC → Stop")
    expect(comparison).to_contain_text("Primera → Intermedia · Variación de saldo -0.01 USDT")
    expect(comparison).to_contain_text("Intermedia → Final · Variación de saldo -2.102 USDT")
    assert timeline["stop_ioc_fill_order"] == "IOC_THEN_STOP"
    assert [step["new_fills"] for step in timeline["intervals"]] == [1, 1]
    assert [step["fill_delta_usdt"] for step in timeline["intervals"]] == ["-0.01", "-2.102"]
    assert all(step["unexplained_cash_delta_usdt"] == "0" for step in timeline["intervals"])
    assert timeline["authorization"] is False
    compare(before, stopped, status=409, final=after)
    expect(comparison).to_contain_text("Muestras incompatibles")
    expect(comparison.locator("table")).to_have_count(0)
    compare(before, after, final=stopped)
    expect(position_row.get_by_role("cell")).to_have_text(["0.5", "0.4", "0"])
    assert len(captures) == 3
    page.set_viewport_size({"width":390,"height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    state = page.request.get(BASE + "/__test/state").json()
    assert (state["bulk_sends"],state["cancel_sends"],state["close_sends"]) == (1,0,0)

    panel.locator(":scope > summary").click()
    expect(probe).to_be_hidden()
    expect(probe.locator('[name="probe_ioc"]')).to_have_value("")
    expect(probe.locator("[data-probe-result]")).to_have_text("")
    expect(comparison.locator('[name="sample_before"]')).to_have_value("")
    expect(comparison.locator('[name="sample_final"]')).to_have_value("")
    expect(comparison.locator("[data-comparison-result]")).to_have_text("")
    page.locator("#real-lock").click()
    expect(probe).to_have_count(0)
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    panel.locator(":scope > summary").click()
    panel.locator('[name="sub_account_id"]').fill(source["sub_account_id"])
    panel.get_by_role("button",name="Consultar",exact=True).click()
    expect(group).to_contain_text("Acuse recibido")
    group.locator("summary").click()
    group.get_by_role("button",name="Muestra de parciales").click()
    expect(probe.locator("[data-probe-archive-state]")).to_have_text("3 guardadas")
    expect(probe.locator('[name="probe_key"]')).to_have_value("")
    comparison.locator("summary").click()
    expect(comparison.locator('[name="probe_source"]')).to_have_value("saved")
    options=probe.locator('[name="saved_sample"] option')
    assert options.count()==4
    ids=[options.nth(index).get_attribute("value") for index in range(1,4)]
    for name,identifier in zip(("saved_before","saved_after","saved_final"),ids,strict=True):
        comparison.locator(f'[name="{name}"]').select_option(identifier)
    with page.expect_response("**/market-comparison") as saved_comparison:
        comparison.get_by_role("button",name="Comparar",exact=True).click()
    assert saved_comparison.value.json()==timeline
    assert saved_comparison.value.request.post_data_json==dict(zip(("before_id","after_id","final_id"),ids,strict=True))
    expect(position_row.get_by_role("cell")).to_have_text(["0.5","0.4","0"])
    storage = page.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
    assert all(secret not in storage for secret in (key, ioc, KEY.hex(), SIGNER))
    assert not errors, errors
    (OUTPUT / "result.json").write_text(json.dumps({"samples":len(captures),"stop_size_before":"0.5",
        "stop_book_size_after":after["stop_book_size"],"position_after":after["remaining_size"],
        "sample_sent_no_orders":True,"discarded_on_close":True,"provider":"CONTROLLED_TESTNET",
        "archive_survives_lock":True,"lost_response_reopened_from_archive":True,"compared_saved_without_credentials":True,
        "compared_native_stop":final_report["after"]["stop_filled"],
        "cash_difference_usdt":final_report["unexplained_cash_delta_usdt"],"reversed_files_rejected":True,
        "three_sample_fill_order":timeline["stop_ioc_fill_order"],"three_sample_intervals":len(timeline["intervals"])}))
    browser.close()
