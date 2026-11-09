"""Browse, export and delete an actual encrypted archive with LAB lock checks."""

import hashlib
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUT = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors, downloads, archive_requests = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("download", lambda file: downloads.append(file.suggested_filename))
    page.on("dialog", lambda dialog: dialog.accept())
    page.on("request", lambda request: archive_requests.append(request.url)
            if "/lab/ai-archive" in request.url else None)
    page.route("**/market/**", lambda route: route.fulfill(status=503, json={}))
    fixture = page.request.get(BASE + "/__test/state").json()
    assert fixture["fixture_id"] == OUT.name
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    assert not archive_requests

    panel = page.locator("#lab-ai-archive-index")
    panel.locator(":scope > summary").click()
    table = panel.get_by_role("table")
    expect(table.locator("tbody tr")).to_have_count(2)
    corpus = panel.locator("[data-ai-archive-jsonl]")
    expect(corpus).to_be_disabled()
    for item in fixture["records"].values():
        table.get_by_role("checkbox", name="Incluir " + item["question"]).check()
    expect(corpus).to_have_text("Exportar JSONL · 2")
    with page.expect_download() as corpus_download:
        corpus.click()
    data = Path(corpus_download.value.path()).read_bytes()
    assert corpus_download.value.suggested_filename == "orbis-ia-" + hashlib.sha256(data).hexdigest()[:12] + ".jsonl"
    samples = [json.loads(line) for line in data.splitlines()]
    assert len(samples) == 2 and data.endswith(b"\n")
    assert {sample["provenance"]["kind"] for sample in samples} == {"research", "backtest"}
    assert {sample["provenance"]["analysis_id"] for sample in samples} == {
        item["id"] for item in fixture["records"].values()}
    assert all(sample["schema"] == "orbis.lab.ai-corpus.v1" and sample["annotation"]["origin"] == "model_generated"
               and not sample["limitations"]["eligible_for_point_in_time_signals"] for sample in samples)
    assert b"sk-owned-browser-fixture" not in data

    page.evaluate(r"""() => {
      const fetchBefore = window.fetch;
      window.fetch = async function(url, options) {
        const response = await fetchBefore.apply(this, arguments);
        if (String(url) !== '/lab/ai-archive/export-jsonl') return response;
        window.fetch = fetchBefore;
        return new Promise((resolve, reject) => {
          document.documentElement.dataset.fixtureCorpusPending = "true";
          options.signal.addEventListener('abort', () => {
            document.documentElement.dataset.fixtureCorpusAborted = "true";
            reject(new DOMException('Archive closed', 'AbortError'));
          }, {once:true});
        });
      };
    }""")
    corpus.click()
    expect(page.locator("html")).to_have_attribute("data-fixture-corpus-pending", "true")
    panel.locator(":scope > summary").click()
    expect(page.locator("html")).to_have_attribute("data-fixture-corpus-aborted", "true")
    assert len(downloads) == 1
    panel.locator(":scope > summary").click()
    expect(table.locator("tbody tr")).to_have_count(2)
    expect(corpus).to_be_disabled()
    panel.get_by_label("Tipo").select_option("research")
    expect(table.locator("tbody tr")).to_have_count(1)
    record = fixture["records"]["research"]
    table.get_by_role("button", name=record["question"]).click()
    detail = panel.locator("[data-ai-archive-detail]")
    expect(detail.get_by_role("heading", name=record["question"])).to_be_focused()
    expect(detail).to_contain_text("Interpretación de prueba de BTC/USDT")
    detail.get_by_text("Evidencia guardada", exact=True).click()
    expect(detail.locator("pre")).to_contain_text('"source_run"')
    panel.screenshot(path=str(OUT / "archive-desktop.png"))

    with page.expect_download() as download:
        detail.get_by_role("button", name="Exportar JSON").click()
    payload = json.loads(Path(download.value.path()).read_text())
    assert payload["schema"] == "orbis.lab.ai-archive.v1" and payload["theoretical_only"]
    [exported] = payload["records"]
    assert exported["id"] == record["id"] and exported["evidence"]["source_run"] == record["run_id"]
    assert "sk-owned-browser-fixture" not in json.dumps(payload)

    table.get_by_role("checkbox", name="Incluir " + record["question"]).check()
    assert page.request.post(BASE + "/lab/lock").ok
    corpus.click()
    expect(panel.get_by_role("status")).to_contain_text("LAB cifrado bloqueado")
    expect(corpus).to_be_disabled()
    assert len(downloads) == 2
    assert page.request.post(BASE + "/lab/unlock", data={"passphrase": fixture["password"]}).ok
    panel.get_by_role("button", name="Actualizar").click()
    table.get_by_role("button", name=record["question"]).click()
    export_individual = detail.get_by_role("button", name="Exportar JSON", exact=True)
    expect(export_individual).to_be_enabled()

    assert page.request.post(BASE + "/lab/lock").ok
    export_individual.click()
    expect(panel.get_by_role("status")).to_contain_text("LAB cifrado bloqueado")
    expect(detail).to_be_hidden()
    expect(table.locator("tbody tr")).to_have_count(0)
    assert len(downloads) == 2
    assert page.request.post(BASE + "/lab/unlock", data={"passphrase": fixture["password"]}).ok
    panel.get_by_role("button", name="Actualizar").click()
    table.get_by_role("button", name=record["question"]).click()
    detail.get_by_role("button", name="Borrar", exact=True).click()
    expect(panel.get_by_role("status")).to_contain_text("Sin análisis")
    assert page.request.get(BASE + "/lab/research/runs/" + record["run_id"]).ok
    assert page.request.get(BASE + "/lab/ai-archive/" + record["id"]).status == 404
    panel.get_by_label("Tipo").select_option("backtest")
    remaining = fixture["records"]["backtest"]
    table.get_by_role("button", name=remaining["question"]).click()
    expect(detail).to_contain_text("Interpretación de prueba")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
    table_scroll = panel.locator(".tablewrap").bounding_box()
    question = table.get_by_role("button", name=remaining["question"]).bounding_box()
    assert table_scroll["x"] <= question["x"] and question["x"] + question["width"] <= table_scroll["x"] + table_scroll["width"]
    panel.screenshot(path=str(OUT / "archive-mobile.png"))
    panel.locator(":scope > summary").click()
    expect(detail).to_be_hidden()
    expect(table.locator("tbody tr")).to_have_count(0)
    assert not errors, errors
    (OUT / "result.json").write_text(json.dumps({
        "passed": True, "archive_reads": len(archive_requests), "no_cached_export_after_lock": True,
        "filtered": True, "deleted_only_analysis": True, "page_errors": errors,
        "jsonl_records": len(samples), "closed_pending_export": True,
    }, indent=2))
    browser.close()
