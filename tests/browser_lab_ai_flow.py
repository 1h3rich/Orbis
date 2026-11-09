"""Browser check of LAB AI consent; the external provider is never contacted."""

from datetime import datetime, timezone
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
prices = [(100, 99), (99, 98), (98, 97), (97, 96), (96, 97), (97, 96),
          (96, 95), (95, 94), (94, 95), (95, 96), (96, 97), (97, 98)]
candles = [{"t": start + i * 3600, "o": str(a), "h": str(max(a, b) + 1),
            "l": str(min(a, b) - 1), "c": str(b), "v": str(10 + i)}
           for i, (a, b) in enumerate(prices)]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    unlocked = page.request.post(BASE + "/lab/unlock", data={
        "passphrase": "browser LAB access password",
    })
    assert unlocked.ok, unlocked.text()
    dataset = page.request.post(BASE + "/lab/datasets", data={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "browser-fixture", "candles": candles,
    })
    assert dataset.ok, dataset.text()
    run = page.request.post(BASE + "/lab/research/runs", data={
        "dataset_id": dataset.json()["id"], "rule": {"kind": "BEARISH_STREAK", "period": 3},
    })
    assert run.ok, run.text()
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-research-select").select_option(run.json()["id"])
    page.wait_for_load_state("networkidle")
    expect(page.locator("#lab-research-report")).to_contain_text("Casos individuales")
    page.locator("#lab-ai-preview-action").click()
    panel = page.locator("#lab-ai-panel")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("Solo se enviarán estos datos agregados")
    expect(panel.locator("pre")).to_contain_text('"schema"')
    preview = page.request.get(BASE + f"/lab/research/runs/{run.json()['id']}/ai-preview").json()
    shown = json.loads(panel.locator("pre").text_content())
    assert shown == preview["evidence"]
    assert preview["archive_available"] is True
    assert panel.locator('[name="save_locally"]').count() == 1
    assert not ({"candles", "cases", "events"} & set(shown))
    assert panel.locator(".lab-ai-result").is_hidden()
    panel.locator('[name="question"]').fill("Revisa 0x" + "a1" * 20)
    panel.locator('[name="api_key"]').fill("sk-browser-test")
    panel.locator('[name="consent"]').check()
    panel.locator('button[type="submit"]').click()
    expect(panel.locator(".lab-ai-result")).to_contain_text("dirección completa")
    assert panel.locator('[name="api_key"]').input_value() == ""
    requests = []

    def mocked_analysis(route):
        payload = route.request.post_data_json
        requests.append(payload)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "answer": "Muestra pequeña; no valida operaciones reales.",
            "model": preview["model"], "evidence_sha256": preview["evidence_sha256"],
            "saved_locally": True,
        }))

    page.route("**/lab/research/runs/*/ai-analyze", mocked_analysis)
    panel.locator('[name="question"]').fill("¿Qué límites tiene esta muestra?")
    panel.locator('[name="api_key"]').fill("sk-browser-test")
    assert panel.locator('button[type="submit"]').is_disabled() is False
    panel.locator('[name="consent"]').check()
    panel.locator('[name="save_locally"]').check()
    panel.locator('button[type="submit"]').click()
    expect(panel.locator(".lab-ai-result")).to_contain_text("Muestra pequeña")
    assert requests[0]["evidence_sha256"] == preview["evidence_sha256"]
    assert requests[0]["api_key"] == "sk-browser-test"
    assert requests[0]["save_locally"] is True
    expect(panel.locator(".lab-ai-result")).to_contain_text("guardado en LAB cifrado")
    assert panel.locator('[name="api_key"]').input_value() == ""
    assert not panel.locator('[name="consent"]').is_checked()
    assert "sk-browser-test" not in page.evaluate("JSON.stringify(localStorage)")
    history_rows = [{"id": "analysis-test", "created_at": "2026-10-20T12:00:00Z",
                     "model": preview["model"], "question": "¿Qué límites tiene esta muestra?",
                     "answer": "Muestra pequeña; no valida operaciones reales.",
                     "hash": "a" * 64, "evidence": preview["evidence"]}]
    page.route("**/lab/research/runs/*/ai-analyses", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(history_rows)))
    page.route("**/lab/research/runs/*/ai-archive", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "schema": "orbis.lab.ai-archive.v1", "environment": "LAB",
            "records": history_rows,
        })))
    removed = []

    def mocked_delete(route):
        removed.append(route.request.post_data_json)
        history_rows.clear()
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"deleted": True, "analysis_id": "analysis-test"}))

    page.route("**/lab/research/runs/*/ai-analyses/*", mocked_delete)
    panel.locator(".lab-ai-history-action").click()
    expect(panel.locator(".lab-ai-history")).to_contain_text("Muestra pequeña")
    page.on("dialog", lambda dialog: dialog.accept())
    with page.expect_download() as downloaded:
        panel.locator(".lab-ai-export-action").click()
    assert json.loads(Path(downloaded.value.path()).read_text())["schema"] == "orbis.lab.ai-archive.v1"
    panel.locator(".lab-ai-history button").click()
    expect(panel.locator(".lab-ai-history")).to_contain_text("No hay análisis guardados")
    assert removed == [{"expected_hash": "a" * 64, "confirmation": "BORRAR ANÁLISIS"}]

    strategy = page.request.post(BASE + "/lab/strategies", data={
        "name": "Tres velas", "definition": {
            "direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
            "fee_rate": "0.001", "slippage_rate": "0", "stop_loss": "0.03",
            "take_profit": "0.08", "entry": {"kind": "BEARISH_STREAK", "period": 3},
        },
    })
    assert strategy.ok, strategy.text()
    backtest = page.request.post(BASE + "/lab/runs", data={
        "dataset_id": dataset.json()["id"], "version_id": strategy.json()["version_id"],
    })
    assert backtest.ok, backtest.text()
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(backtest.json()["id"])
    page.locator("#lab-run-ai-action").click()
    run_panel = page.locator("#lab-run-ai-panel")
    expect(run_panel).to_be_visible()
    run_preview = page.request.get(BASE + f"/lab/runs/{backtest.json()['id']}/ai-preview").json()
    shown_run = json.loads(run_panel.locator("pre").text_content())
    assert shown_run == run_preview["evidence"]
    assert shown_run["metrics"]["trades"] == len(backtest.json()["trades"])
    assert not ({"candles", "signals", "trades", "equity"} & set(shown_run))
    run_requests = []

    def mocked_backtest(route):
        run_requests.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "answer": "Backtest teórico; revisar fuera de muestra.",
            "model": run_preview["model"], "evidence_sha256": run_preview["evidence_sha256"],
        }))

    page.route("**/lab/runs/*/ai-analyze", mocked_backtest)
    run_panel.locator('[name="question"]').fill("¿Qué límites tiene este backtest?")
    run_panel.locator('[name="api_key"]').fill("sk-browser-backtest")
    run_panel.locator('[name="consent"]').check()
    run_panel.locator('button[type="submit"]').click()
    expect(run_panel.locator(".lab-ai-result")).to_contain_text("Backtest teórico")
    assert run_requests[0]["evidence_sha256"] == run_preview["evidence_sha256"]
    assert run_panel.locator('[name="api_key"]').input_value() == ""
    assert "sk-browser-backtest" not in page.evaluate("JSON.stringify(localStorage)")
    assert not errors, errors
    browser.close()
    print("Browser LAB AI flow passed: research and backtest preview, consent, ephemeral key, no external request")
