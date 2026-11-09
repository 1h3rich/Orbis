"""La cabecera de privacidad distingue bases en claro, incertidumbre y cifrado.

Sólo intercepta lecturas de estado en el navegador; no modifica bases locales.
Ejecutar con el servidor de Orbis disponible en 127.0.0.1:8000.
"""

import json

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8000/ui/#/config"
STATUS_PATHS = {
    "/paper/legacy/status": "legacy",
    "/lab/status": "lab",
    "/automation/dca/status": "automation",
    "/paper/spot/strategies/status": "spot",
    "/paper/perp/status": "perp",
}


def check_case(browser, formats, expected, *, error=None):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    def mock_status(route):
        path = route.request.url.split("?", 1)[0].split(":8000", 1)[-1]
        name = STATUS_PATHS.get(path)
        if name is None:
            route.continue_()
            return
        if name == error:
            route.fulfill(status=503, content_type="application/json", body='{"detail":"indisponible"}')
            return
        body = {"format": formats[name], "unlocked": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/*/status", mock_status)
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    summary = page.locator("#cfg-privacy-summary")
    expect(summary).to_contain_text(expected)
    expect(summary).not_to_contain_text("Comprobando almacenes")
    assert not errors, errors
    page.close()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    all_plain = dict.fromkeys(STATUS_PATHS.values(), "plain")
    check_case(browser, all_plain, "5 almacén(es) sin cifrar")
    check_case(browser, all_plain, "PAPER manual · LAB · DCA · Spot PAPER · GRVT PAPER")
    mixed = dict.fromkeys(STATUS_PATHS.values(), "encrypted_or_invalid")
    mixed["lab"] = "plain"
    check_case(browser, mixed, "1 almacén(es) sin cifrar: LAB")
    mixed["lab"] = "encrypted_or_invalid"
    check_case(browser, mixed, "Sin SQLite en claro")
    check_case(browser, mixed, "Estado de cifrado no verificable: LAB", error="lab")
    browser.close()
