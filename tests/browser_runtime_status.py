"""La UI avisa de un backend viejo o modificado sin reiniciarlo."""

import json

from playwright.sync_api import expect, sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    for width in (1440, 390):
        page = browser.new_page(viewport={"width": width, "height": 850})
        state = {"status": 404, "restart_required": None}

        def runtime(route):
            if state["status"] == 404:
                route.fulfill(status=404, content_type="application/json",
                              body=json.dumps({"detail": "Not Found"}))
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"restart_required": state["restart_required"]}))

        page.route("**/system/runtime", runtime)
        page.goto("http://127.0.0.1:8000/ui/#/inicio")
        badge = page.locator("#runtime-status")
        expect(badge).to_have_text("Servidor sin verificar")
        assert "reinicia ORBIS" in badge.get_attribute("title")
        state.update(status=200, restart_required=False)
        page.reload()
        expect(badge).to_be_hidden()
        state["restart_required"] = True
        page.reload()
        expect(badge).to_have_text("Reinicio pendiente")
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.close()
    browser.close()
    print("Runtime status UI passed: legacy, current, changed, mobile")
