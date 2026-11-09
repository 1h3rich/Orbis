"""Review, send and archive a two-run comparison through the real LAB UI."""

import hashlib
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ["ORBIS_TEST_BASE_URL"]
OUT = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])
KEY = "sk-owned-browser-fixture"
QUESTION = "Contrasta Base y Alternativa"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors, submissions = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.on("request", lambda request: submissions.append(request.post_data_json)
            if request.url.endswith("/runs/compare/ai-analyze") else None)
    page.route("**/market/**", lambda route: route.fulfill(status=503, json={}))
    fixture = page.request.get(BASE + "/__test/state").json()
    assert fixture["fixture_id"] == OUT.name
    pair = fixture["comparison"]
    page.goto(BASE + "/ui/#/laboratorio")
    left, right = page.locator("#lab-compare-left"), page.locator("#lab-compare-right")

    def choose_pair():
        left.select_option(pair["left_id"])
        right.select_option(pair["right_id"])
        page.locator("#lab-compare-action").click()
        expect(page.locator("#lab-comparison")).to_contain_text("VERIFICADOS")
        expect(page.get_by_role("button", name="Analizar comparación con IA")).to_be_visible()

    choose_pair()
    page.evaluate("""() => {
      const next = window.fetch;
      window.fetch = async function(url) {
        const reply = await next.apply(this, arguments);
        if (String(url) !== "/lab/runs/compare/ai-preview") return reply;
        window.fetch = next;
        return new Promise(resolve => {
          window.releaseComparisonPreview = () => resolve(reply);
        });
      };
    }""")
    page.get_by_role("button", name="Analizar comparación con IA").click()
    page.wait_for_function("() => typeof window.releaseComparisonPreview === 'function'")
    right.select_option(pair["left_id"])
    page.evaluate("window.releaseComparisonPreview()")
    expect(page.locator("#lab-compare-ai-panel")).to_have_count(0)
    assert submissions == []

    choose_pair()
    page.get_by_role("button", name="Analizar comparación con IA").click()
    panel = page.locator("#lab-compare-ai-panel")
    panel.get_by_text("Ver datos exactos a enviar", exact=True).click()
    evidence = json.loads(panel.locator("pre").inner_text())
    assert [item["run_id"] for item in evidence["runs"]] == list(pair.values())
    assert evidence["comparison"]["comparable"] and not evidence["comparison"]["assumptions_equal"]
    assert "Alternativa LAB" not in json.dumps(evidence)
    panel.get_by_label("Pregunta", exact=True).fill(QUESTION)
    panel.get_by_label("Clave API OpenAI").fill(KEY)
    panel.get_by_label("Guardar respuesta en LAB cifrado").check()
    submit = panel.get_by_role("button", name="Enviar análisis")
    submit.click()
    assert submissions == []  # Checkbox required and the request is never started.
    panel.get_by_label("Confirmo el envío externo", exact=False).check()
    submit.click()
    result = panel.locator(".lab-ai-result")
    expect(result).to_contain_text("Comparación de prueba: Base y Alternativa.")
    expect(result).to_contain_text("guardado en LAB cifrado")
    expect(panel.get_by_label("Clave API OpenAI")).to_have_value("")
    assert len(submissions) == 1
    assert submissions[0]["confirmation"] == "ENVIAR COMPARACIÓN A OPENAI"
    assert [submissions[0][field] for field in pair] == list(pair.values())
    assert KEY not in page.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")

    page.reload()
    archive = page.locator("#lab-ai-archive-index")
    archive.locator(":scope > summary").click()
    row = archive.get_by_role("row").filter(has_text=QUESTION)
    expect(row).to_contain_text("Comparación")
    row.get_by_role("button", name=QUESTION).click()
    detail = archive.locator("[data-ai-archive-detail]")
    expect(detail).to_contain_text(pair["left_id"][:8] + " ↔ " + pair["right_id"][:8])
    expect(detail).to_contain_text("Comparación de prueba")
    row.get_by_role("checkbox").check()
    with page.expect_download() as download:
        archive.locator("[data-ai-archive-jsonl]").click()
    data = Path(download.value.path()).read_bytes()
    assert download.value.suggested_filename.endswith(hashlib.sha256(data).hexdigest()[:12] + ".jsonl")
    sample = json.loads(data)
    assert sample["input"]["evidence"] == evidence and KEY.encode() not in data
    for selector, button, panel_id, run_id in (
        ("#lab-run-select", "#lab-run-ai-action", "#lab-run-ai-panel", pair["left_id"]),
        ("#lab-research-select", "#lab-ai-preview-action", "#lab-ai-panel", fixture["records"]["research"]["run_id"]),
    ):
        page.locator(selector).select_option(run_id)
        page.locator(button).click()
        single = page.locator(panel_id)
        single.get_by_label("Pregunta", exact=True).fill("Revisa los límites del caso")
        single.get_by_label("Clave API OpenAI").fill(KEY)
        single.get_by_label("Confirmo el envío externo", exact=False).check()
        single.get_by_role("button", name="Enviar análisis").click()
        expect(single.locator(".lab-ai-result")).to_contain_text("Interpretación de prueba de BTC/USDT")
        expect(single.locator(".lab-ai-result")).to_contain_text("no guardado")
        expect(single.get_by_label("Clave API OpenAI")).to_have_value("")
    assert not errors, errors
    (OUT / "comparison-ai-result.json").write_text(json.dumps({
        "verified_pair": True, "stale_preview_discarded": True, "provider_requests": len(submissions),
        "encrypted_archive_reopened": True, "jsonl_exact": True, "page_errors": errors,
    }))
    browser.close()
    print("LAB AI comparison: reviewed pair, explicit consent, encrypted archive and JSONL passed.")
