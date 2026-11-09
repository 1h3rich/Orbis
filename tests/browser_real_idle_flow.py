"""Prueba de navegador con bóveda aislada: inactividad REAL y revocación."""

from playwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.clock.install()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser idle test 123")
    page.locator("#real-passphrase-confirm").fill("browser idle test 123")
    with page.expect_response("**/real/vault/create") as created:
        page.get_by_role("button", name="Crear bóveda").click()
    old_token = created.value.json()["token"]
    page.locator("#real-new-account").wait_for()
    page.locator('#real-new-account [name="name"]').fill("Idle test")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-event-form").wait_for()
    page.locator('#real-event-form [name="quantity"]').fill("0.125")
    page.locator('#real-event-form [name="unit_price"]').fill("100.50")
    page.get_by_role("button", name="Guardar movimiento").click()
    page.get_by_text("0.125", exact=True).first.wait_for()

    # Una petición autenticada reciente renueva el plazo visible, igual que en el servidor.
    page.clock.fast_forward(600_000)
    page.get_by_role("button", name="Ocultar importes").click()
    page.get_by_role("button", name="Mostrar importes").wait_for()
    page.clock.fast_forward(894_000)
    assert page.locator("#real-event-form").count() == 1

    page.clock.fast_forward(2_000)
    page.locator("#real-passphrase").wait_for()
    assert page.locator("#real-positions").count() == 0
    assert page.locator("#real-wallet-root").count() == 0
    assert "0.125" not in page.locator("body").inner_text()
    response = page.request.get("http://127.0.0.1:8799/real/accounts", headers={"X-Orbis-Vault-Token": old_token})
    assert response.status == 401, response.text()

    # Una respuesta válida iniciada antes del bloqueo no puede repintar el libro.
    page.locator("#real-passphrase").fill("browser idle test 123")
    with page.expect_response("**/real/vault/unlock") as unlocked:
        page.get_by_role("button", name="Desbloquear").click()
    new_token = unlocked.value.json()["token"]
    page.get_by_role("button", name="Mostrar importes").click()
    page.get_by_text("0.125", exact=True).first.wait_for()
    snapshot = page.request.get(
        "http://127.0.0.1:8799/real/accounts/" +
        page.locator("#real-account").input_value() + "/snapshot",
        headers={"X-Orbis-Vault-Token": new_token},
    )
    assert snapshot.ok
    page.evaluate("""body => {
      const originalFetch = window.fetch;
      window.fetch = (input, options) => {
        if (!window.__heldRealSnapshot && String(input).includes('/snapshot')) {
          window.__heldRealSnapshot = true;
          return new Promise(resolve => {
            window.__releaseRealSnapshot = () => resolve(new Response(body, {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
          });
        }
        return originalFetch(input, options);
      };
    }""", snapshot.text())
    page.get_by_role("button", name="Ocultar importes").click()
    page.wait_for_function("() => window.__heldRealSnapshot === true")
    page.clock.fast_forward(896_000)
    page.locator("#real-passphrase").wait_for()
    page.evaluate("window.__releaseRealSnapshot()")
    assert "0.125" not in page.locator("body").inner_text()
    assert page.locator("#real-positions").count() == 0
    assert not errors, errors
    print("Browser REAL idle flow passed: renewal, auto-lock, token revoked, stale response ignored")
    browser.close()
