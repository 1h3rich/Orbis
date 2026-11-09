"""Destino temporal ETH/ERC-20 cotejado con el contrato de la wallet."""

from playwright.sync_api import expect, sync_playwright


PROVIDER = r"""
(() => {
  const listeners = {};
  const wallet = {address: "0x1111111111111111111111111111111111111111", chain: "0x1", calls: []};
  wallet.emit = (name, value) => (listeners[name] || []).forEach(fn => fn(value));
  wallet.provider = {
    request: async ({method, params}) => {
      wallet.calls.push(method);
      if (method === "eth_requestAccounts" || method === "eth_accounts") return [wallet.address];
      if (method === "eth_chainId") return wallet.chain;
      if (method === "eth_blockNumber") return "0x1234";
      if (method === "eth_getBlockByNumber") return {
        number: params[0], hash: "0x" + "ab".repeat(32),
        timestamp: "0x" + Math.floor(Date.now() / 1000).toString(16)
      };
      if (method === "eth_getBalance") return "0x0";
      if (method === "eth_call" && params[0].data === "0x313ce567") return "0x" + "6".padStart(64, "0");
      if (method === "eth_call" && params[0].data.startsWith("0x70a08231")) return "0x" + "1".padStart(64, "0");
      throw new Error("Unexpected wallet method: " + method);
    },
    on: (name, fn) => { (listeners[name] ||= []).push(fn); },
    removeListener: (name, fn) => { listeners[name] = (listeners[name] || []).filter(item => item !== fn); }
  };
  window.testWallet = wallet;
  window.addEventListener("eip6963:requestProvider", () => {
    window.dispatchEvent(new CustomEvent("eip6963:announceProvider", {detail: {
      info: {uuid: "11111111-1111-4111-8111-111111111111", name: "Wallet prueba"}, provider: wallet.provider
    }}));
  });
})();
"""


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    page.add_init_script(PROVIDER)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8799/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser destination secret 123")
    page.locator("#real-passphrase-confirm").fill("browser destination secret 123")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator('#real-new-account [name="name"]').fill("MEXC")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-mexc-connect summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("browser-test-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("browser-test-secret")
    page.get_by_role("button", name="Consultar MEXC").click()
    page.locator("#real-mexc-link > summary").click()
    expect(page.locator("#real-mexc-link-form")).to_be_visible()
    page.locator('#real-mexc-link-form [name="confirmation"]').fill("VINCULAR")
    page.get_by_role("button", name="Vincular libro").click()
    expect(page.locator("#real-mexc-link-state")).to_contain_text("Vínculo local guardado")
    page.locator("#real-wallet-connect").click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    page.locator("#real-wallet-token-panel summary").click()
    page.locator("#real-wallet-token-contract").fill("0x" + "44" * 20)
    page.locator("#real-wallet-token-submit").click()
    expect(page.locator("#real-wallet-token-state")).to_contain_text("Saldo observado")
    deposit = page.locator("#real-wallet-deposit-panel")
    expect(deposit).to_be_visible()
    deposit.locator("summary").click()
    deposit.locator('input[name="asset"]').fill("USDT")
    with page.expect_response("**/mexc-erc20-deposit-destination?asset=USDT") as response:
        deposit.get_by_role("button", name="Consultar destino").click()
    assert response.value.status == 200, response.value.text()
    address = "0x" + "33" * 20
    expect(page.locator(".real-wallet-deposit-address")).to_have_text(address)
    expect(page.locator("#real-wallet-deposit-state")).to_contain_text("Orbis no transfiere fondos")
    page.screenshot(path="/tmp/orbis-mexc-deposit-destination-ui.png", full_page=True)
    assert address not in page.evaluate("JSON.stringify(window.localStorage)")
    assert "eth_sendTransaction" not in page.evaluate("window.testWallet.calls")
    page.locator("#real-wallet-token-contract").fill("0x" + "55" * 20)
    page.locator("#real-wallet-token-submit").click()
    expect(page.locator(".real-wallet-deposit-address")).to_have_count(0)
    deposit.locator('input[name="asset"]').fill("USDT")
    deposit.get_by_role("button", name="Consultar destino").click()
    expect(page.locator("#real-wallet-deposit-state")).to_contain_text("contrato de la wallet no coincide")
    expect(page.locator(".real-wallet-deposit-address")).to_have_count(0)
    page.evaluate("window.testWallet.chain = '0x89'; window.testWallet.emit('chainChanged', '0x89')")
    expect(deposit).to_be_hidden()
    assert address not in page.locator("body").inner_text()
    assert not errors, errors
    browser.close()
    print("MEXC ERC-20 destination browser flow passed: contract gate, no sending or storage")
