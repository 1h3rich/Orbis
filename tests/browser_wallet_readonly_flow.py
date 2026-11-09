"""Wallet EIP-6963 ficticia en navegador; sin extensiones, fondos ni RPC externos."""

import os

from playwright.sync_api import expect, sync_playwright
from eth_account import Account
from eth_account.messages import encode_defunct


SIGNER_KEY = bytes.fromhex("11" * 32)
SIGNER_ADDRESS = Account.from_key(SIGNER_KEY).address
BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")


FAKE_WALLETS = r"""
(() => {
  const providers = [
    {uuid: "11111111-1111-4111-8111-111111111111", name: "MetaMask", address: "0x1111111111111111111111111111111111111111", other: "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", chain: "0x1", balance: "0xde0b6b3a7640000"},
    {uuid: "22222222-2222-4222-8222-222222222222", name: "Trust Wallet", address: "0x2222222222222222222222222222222222222222", chain: "0x89", balance: "0x1bc16d674ec80000"},
    {uuid: "33333333-3333-4333-8333-333333333333", name: "Test signer", address: "__SIGNER_ADDRESS__", chain: "0x1", balance: "0xde0b6b3a7640000", signer: true}
  ];
  window.walletProbe = {calls: [], providers};
  for (const wallet of providers) {
    const listeners = {};
    wallet.emit = (kind, value) => (listeners[kind] || []).forEach(fn => fn(value));
    const provider = {
      request: async ({method, params}) => {
        window.walletProbe.calls.push({name: wallet.name, method, params});
        if (method === "eth_requestAccounts") return wallet.other ? [wallet.address, wallet.other] : [wallet.address];
        if (method === "eth_accounts") return wallet.other ? [wallet.address, wallet.other] : [wallet.address];
        if (method === "eth_chainId") return wallet.chain;
        if (method === "eth_getBalance") {
          if (wallet.pauseBalance) return new Promise(resolve => {
            wallet.resumeBalance = () => { wallet.pauseBalance = false; resolve(wallet.balance); };
          });
          return wallet.balance;
        }
        if (method === "personal_sign" && wallet.signer) return window.testSign(params[0]);
        if (method === "eth_blockNumber") return "0x1234";
        if (method === "eth_getBlockByNumber") return {
          number: params[0], hash: "0x" + "ab".repeat(32),
          timestamp: "0x" + (Math.floor(Date.now() / 1000) - (wallet.blockAgeSeconds || 0)).toString(16)
        };
        if (method === "eth_call") {
          if (params[0].to.toLowerCase() === "0x61ffe014ba17989e743c5f6cb21bf9697530b21e") {
            if (wallet.quoterFails) throw new Error("No pool");
            const fee = Number(BigInt("0x" + params[0].data.slice(202, 266)));
            if (fee === 10000) throw new Error("Pool unavailable");
            const output = fee === 500 ? 2500000000000000000n :
              fee === 100 ? 2100000000000000000n : 2000000000000000000n;
            const response = "0x" + [output, 2n, 1n, 120000n]
              .map(value => value.toString(16).padStart(64, "0")).join("");
            if (wallet.pauseQuote) return new Promise(resolve => {
              wallet.resumeQuote = () => { wallet.pauseQuote = false; resolve(response); };
            });
            return response;
          }
          if (params[0].data === "0x313ce567") {
            if (wallet.noDecimals) throw new Error("optional decimals unavailable");
            if (wallet.pauseDecimals) return new Promise(resolve => {
              wallet.resumeDecimals = () => { wallet.pauseDecimals = false; resolve("0x" + (wallet.tokenDecimals ?? 6).toString(16).padStart(64, "0")); };
            });
            return "0x" + (wallet.tokenDecimals ?? 6).toString(16).padStart(64, "0");
          }
          if (params[0].data.startsWith("0xdd62ed3e")) {
            if (wallet.allowanceFails) throw new Error("allowance unavailable");
            return "0x" + BigInt(wallet.routerAllowance ?? "2000000000000000000").toString(16).padStart(64, "0");
          }
          if (params[0].data.startsWith("0x70a08231")) return "0x" + BigInt(wallet.tokenRaw ?? "123456789").toString(16).padStart(64, "0");
        }
        throw new Error("Unexpected method: " + method);
      },
      on: (kind, fn) => { (listeners[kind] ||= []).push(fn); },
      removeListener: (kind, fn) => { listeners[kind] = (listeners[kind] || []).filter(value => value !== fn); }
    };
    wallet.listeners = listeners;
    wallet.provider = provider;
  }
  window.addEventListener("eip6963:requestProvider", () => {
    for (const wallet of providers) {
      window.dispatchEvent(new CustomEvent("eip6963:announceProvider", {
        detail: {info: {uuid: wallet.uuid, name: wallet.name, icon: "", rdns: "test.wallet"}, provider: wallet.provider}
      }));
    }
  });
})();
"""


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.expose_function("testSign", lambda encoded: "0x" + Account.sign_message(
        encode_defunct(hexstr=encoded), SIGNER_KEY).signature.hex())
    page.add_init_script(FAKE_WALLETS.replace("__SIGNER_ADDRESS__", SIGNER_ADDRESS))
    errors = []
    http_requests = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: http_requests.append(request.url + " " + (request.post_data or "")))
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.locator("#real-passphrase-confirm").fill("browser smoke secret 123")
    page.get_by_role("button", name="Crear bóveda").click()
    page.locator("#real-new-account").wait_for()
    page.locator('#real-new-account [name="name"]').fill("Principal")
    page.get_by_role("button", name="Crear cuenta").click()
    page.locator("#real-wallet-provider option").nth(1).wait_for(state="attached")
    assert page.evaluate("window.walletProbe.calls.length") == 0
    page.locator("#real-wallet-provider").select_option(label="MetaMask")
    page.locator("#real-wallet-connect").click()
    page.get_by_text("Saldo actualizado.").wait_for()
    wallet_state = page.locator("#real-wallet-state")
    assert "0x1111…1111" in wallet_state.inner_text()
    assert "Ethereum · 1.0 ETH" in wallet_state.inner_text()
    assert "bloque 4660" in wallet_state.inner_text()
    comparison = page.locator("#real-wallet-compare")
    expect(comparison).to_be_visible()
    comparison.locator("summary").click()
    expect(page.locator("#real-wallet-compare-state")).to_contain_text(
        "Wallet 1.0 ETH · libro 0.0 ETH · diferencia 1.0 ETH")
    page.evaluate("window.OrbisWallet.setBookSnapshot({cash:{ETH:'1E-18'},positions:[{asset:'ETH',quantity:'0.999999999999999999'}]})")
    expect(page.locator("#real-wallet-compare-state")).to_contain_text(
        "diferencia 0.0 ETH · misma cantidad, no conciliada")
    trade = page.locator("#real-event-form")
    trade.locator('[name="asset"]').fill("ETH")
    trade.locator('[name="quantity"]').fill("1")
    trade.locator('[name="unit_price"]').fill("100")
    trade.get_by_role("button", name="Guardar movimiento").click()
    expect(page.locator("#real-wallet-compare-state")).to_contain_text(
        "Wallet 1.0 ETH · libro 1.0 ETH · diferencia 0.0 ETH")
    assert "0x1111111111111111111111111111111111111111" not in page.locator("body").inner_text()
    assert page.evaluate("window.walletProbe.calls.map(item => item.method)") == [
        "eth_requestAccounts", "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber",
        "eth_getBalance", "eth_chainId"
    ]
    assert page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_getBalance')[0].params[1]") == "0x1234"
    page.evaluate("window.walletProbe.providers[0].blockAgeSeconds = 3600")
    page.locator("#real-wallet-refresh").click()
    expect(wallet_state).to_contain_text("RPC atrasado: saldo no actualizado")
    expect(comparison).to_be_hidden()
    page.evaluate("window.walletProbe.providers[0].blockAgeSeconds = 0")
    page.locator("#real-wallet-refresh").click()
    expect(wallet_state).to_contain_text("Saldo actualizado")
    page.evaluate("window.walletProbe.providers[0].pauseBalance = true")
    page.locator("#real-wallet-refresh").click()
    expect(wallet_state).to_contain_text("Consultando la red")
    page.wait_for_function("Boolean(window.walletProbe.providers[0].resumeBalance)")
    page.evaluate("window.walletProbe.providers[0].chain = '0x89'; window.walletProbe.providers[0].resumeBalance()")
    expect(wallet_state).to_contain_text("No se pudo consultar el saldo")
    expect(comparison).to_be_hidden()
    assert "1.0 ETH" not in wallet_state.inner_text()
    page.evaluate("window.walletProbe.providers[0].chain = '0x1'")
    page.locator("#real-wallet-refresh").click()
    expect(wallet_state).to_contain_text("Saldo actualizado.")
    page.locator("#real-wallet-account").select_option("1")
    expect(wallet_state).to_contain_text("0xaaaa…aaaa")
    assert page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_getBalance').at(-1).params[0]") == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    page.locator("#real-wallet-token-panel summary").click()
    contract = "0x3333333333333333333333333333333333333333"
    page.locator("#real-wallet-token-contract").fill(contract)
    page.locator("#real-wallet-token-submit").click()
    token_state = page.locator("#real-wallet-token-state")
    expect(token_state).to_contain_text("123.456789 unidades · bloque 4660")
    token_calls = page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call')")
    assert len(token_calls) == 2
    assert all(call["params"][0]["to"] == contract and call["params"][1] == "0x1234" for call in token_calls)
    assert token_calls[1]["params"][0]["data"] == "0x70a08231" + ("a" * 40).rjust(64, "0")
    page.evaluate("window.walletProbe.providers[0].blockAgeSeconds = 3600")
    page.locator("#real-wallet-token-submit").click()
    expect(token_state).to_contain_text("RPC atrasado: token no actualizado")
    assert len(page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call')")) == 2
    page.evaluate("window.walletProbe.providers[0].blockAgeSeconds = 0")
    page.locator("#real-wallet-token-submit").click()
    expect(token_state).to_contain_text("Saldo observado")
    page.evaluate("window.walletProbe.providers[0].noDecimals = true")
    page.locator("#real-wallet-token-submit").click()
    expect(token_state).to_contain_text("123456789 unidades mínimas")
    page.evaluate("window.walletProbe.providers[0].noDecimals = false; window.walletProbe.providers[0].tokenDecimals = 18; window.walletProbe.providers[0].tokenRaw = '123456789012345678901234567890'")
    page.locator("#real-wallet-token-submit").click()
    expect(token_state).to_contain_text("123456789012.34567890123456789 unidades")
    dex = page.locator("#real-wallet-dex-panel")
    expect(dex).to_be_visible()
    dex.locator("summary").click()
    dex.locator('[name="token_out"]').fill("0x4444444444444444444444444444444444444444")
    dex.locator('[name="amount_in"]').fill("1.0")
    dex.locator('[name="pool_fee"]').select_option("3000")
    dex.locator("#real-wallet-dex-submit").click()
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("pool 0,3 % · bloque 4660 · salida 2.0 unidades de 0x4444…4444 · mínimo orientativo 1.99")
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("Permiso ERC-20 de SwapRouter02: 2.0 unidades (cubre esta cantidad)")
    allowance_calls = page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call' && item.params[0].data.startsWith('0xdd62ed3e'))")
    assert len(allowance_calls) == 1
    assert allowance_calls[0]["params"][0]["to"] == contract
    assert allowance_calls[0]["params"][1] == "0x1234"
    assert allowance_calls[0]["params"][0]["data"] == "0xdd62ed3e" + ("a" * 40).rjust(64, "0") + "68b3465833fb72a70ecdf485e0e4c7bd8665fc45".rjust(64, "0")
    quoter_calls = page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call' && item.params[0].to.toLowerCase() === '0x61ffe014ba17989e743c5f6cb21bf9697530b21e')")
    assert len(quoter_calls) == 1
    assert quoter_calls[0]["params"][1] == "0x1234"
    expected_words = [int("33" * 20, 16), int("44" * 20, 16), 10**18, 3000, 0]
    assert quoter_calls[0]["params"][0]["data"] == "0xc6a5026a" + "".join(
        f"{value:064x}" for value in expected_words
    )
    assert "eth_sendTransaction" not in page.evaluate("window.walletProbe.calls.map(item => item.method)")
    page.evaluate("window.walletProbe.providers[0].routerAllowance = '0'")
    dex.locator("#real-wallet-dex-submit").click()
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("Permiso ERC-20 de SwapRouter02: 0.0 unidades (no cubre esta cantidad)")
    page.evaluate("window.walletProbe.providers[0].allowanceFails = true")
    dex.locator("#real-wallet-dex-submit").click()
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("Permiso ERC-20 de SwapRouter02 no verificable")
    page.evaluate("window.walletProbe.providers[0].allowanceFails = false")
    dex.locator('[name="pool_fee"]').select_option("auto")
    dex.locator("#real-wallet-dex-submit").click()
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("pool 0,05 % · bloque 4660 · salida 2.5 unidades de 0x4444…4444")
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("Mayor salida bruta entre 3/4 respuestas")
    assert len(page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call' && item.params[0].to.toLowerCase() === '0x61ffe014ba17989e743c5f6cb21bf9697530b21e')")) == 7
    page.evaluate("window.walletProbe.providers[0].blockAgeSeconds = 3600")
    dex.locator("#real-wallet-dex-submit").click()
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("RPC desactualizado")
    assert len(page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call' && item.params[0].to.toLowerCase() === '0x61ffe014ba17989e743c5f6cb21bf9697530b21e')")) == 7
    page.evaluate("window.walletProbe.providers[0].blockAgeSeconds = 0")
    page.evaluate("window.walletProbe.providers[0].quoterFails = true")
    dex.locator("#real-wallet-dex-submit").click()
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("No se pudo verificar la cotización")
    page.evaluate("window.walletProbe.providers[0].quoterFails = false")
    dex.locator('[name="pool_fee"]').select_option("3000")
    page.evaluate("window.walletProbe.providers[0].pauseQuote = true")
    dex.locator("#real-wallet-dex-submit").click()
    page.wait_for_function("Boolean(window.walletProbe.providers[0].resumeQuote)")
    dex.locator('[name="amount_in"]').fill("2.0")
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("Cotización sin consultar")
    page.evaluate("window.walletProbe.providers[0].resumeQuote()")
    page.wait_for_timeout(50)
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("Cotización sin consultar")
    page.evaluate("window.walletProbe.providers[0].tokenRaw = '0'")
    dex.locator("#real-wallet-dex-submit").click()
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("Saldo insuficiente")
    assert len(page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call' && item.params[0].to.toLowerCase() === '0x61ffe014ba17989e743c5f6cb21bf9697530b21e')")) == 12
    page.evaluate("window.walletProbe.providers[0].tokenRaw = '123456789012345678901234567890'")
    page.screenshot(path="/tmp/orbis-wallet-readonly-ui.png", full_page=True)
    page.get_by_role("button", name="Ocultar importes").click()
    expect(wallet_state).to_contain_text("••••")
    page.locator("#real-wallet-compare summary").click()
    expect(page.locator("#real-wallet-compare-state")).to_contain_text("Importes ocultos")
    assert "Wallet 1.0 ETH" not in page.locator("#real-wallet-compare-state").inner_text()
    assert "••••" in wallet_state.inner_text()
    expect(token_state).to_contain_text("••••")
    if not dex.evaluate("element => element.open"):
        dex.locator("summary").click()
    dex.locator('[name="token_out"]').fill("0x4444444444444444444444444444444444444444")
    dex.locator('[name="amount_in"]').fill("1.0")
    dex.locator("#real-wallet-dex-submit").click()
    expect(page.locator("#real-wallet-dex-state")).to_contain_text("importe oculto")
    assert " · 1.0" not in wallet_state.inner_text()
    balance_calls_before = page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call' && item.params[0].data.startsWith('0x70a08231')).length")
    page.evaluate("window.walletProbe.providers[0].pauseDecimals = true")
    page.locator("#real-wallet-token-submit").click()
    expect(token_state).to_contain_text("Consultando token…")
    for _ in range(50):
        if page.evaluate("Boolean(window.walletProbe.providers[0].resumeDecimals)"):
            break
        page.wait_for_timeout(20)
    assert page.evaluate("Boolean(window.walletProbe.providers[0].resumeDecimals)")
    page.evaluate("window.walletProbe.providers[0].chain = '0x89'; window.walletProbe.providers[0].emit('chainChanged', '0x89')")
    expect(wallet_state).to_contain_text("Polygon · •••• POL")
    page.evaluate("window.walletProbe.providers[0].resumeDecimals()")
    page.wait_for_timeout(50)
    assert page.evaluate("window.walletProbe.calls.filter(item => item.method === 'eth_call' && item.params[0].data.startsWith('0x70a08231')).length") == balance_calls_before
    assert "123456789012" not in token_state.inner_text()
    assert page.locator("#real-wallet-token-contract").input_value() == ""
    page.locator("#real-wallet-disconnect").click()
    assert "Desconectada." in wallet_state.inner_text()
    page.locator("#real-wallet-provider").select_option(label="Trust Wallet")
    page.locator("#real-wallet-connect").click()
    page.get_by_text("Saldo actualizado.").wait_for()
    assert "0x2222…2222" in wallet_state.inner_text()
    assert "Polygon · •••• POL" in wallet_state.inner_text()
    page.evaluate("window.walletProbe.providers[1].chain = '0x1'; window.walletProbe.providers[1].emit('chainChanged', '0x1')")
    page.get_by_text("Ethereum · •••• ETH").wait_for()
    assert "123456789" not in page.locator("body").inner_text()
    page.evaluate("window.walletProbe.providers[1].chain = '0xabcdef'; window.walletProbe.providers[1].emit('chainChanged', '0xabcdef')")
    page.get_by_text("Red EVM 0xabcdef · •••• wei").wait_for()
    page.evaluate("window.walletProbe.providers[1].emit('accountsChanged', [])")
    assert "Desconectada." in wallet_state.inner_text()
    page.locator("#real-wallet-provider").select_option(label="Test signer")
    page.locator("#real-wallet-connect").click()
    page.get_by_text("Saldo actualizado.").wait_for()
    assert "Sin verificar control" in page.locator("#real-wallet-proof-state").inner_text()
    def accept_control(dialog):
        assert "NO autoriza pagos" in dialog.message
        assert SIGNER_ADDRESS.lower() in dialog.message
        dialog.accept()
    page.once("dialog", accept_control)
    page.locator("#real-wallet-proof").click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    assert page.evaluate("window.walletProbe.calls.filter(item => item.method === 'personal_sign').length") == 1
    page.locator("#real-wallet-links summary").click()
    page.locator("#real-wallet-link-alias").fill("Firma de prueba")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#real-wallet-link-save").click()
    expect(page.locator("#real-wallet-links-list")).to_contain_text("Firma de prueba · 0x1")
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("corresponde a esta cuenta REAL")
    expect(page.locator("#real-wallet-link-form")).to_be_hidden()
    assert SIGNER_ADDRESS.lower() not in page.locator("#real-wallet-links-list").inner_text().lower()
    page.evaluate("window.walletProbe.providers[2].chain = '0x89'; window.walletProbe.providers[2].emit('chainChanged', '0x89')")
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Sin verificar control")
    expect(page.locator("#real-wallet-link-form")).to_be_hidden()
    page.get_by_role("button", name="Bloquear").click()
    page.locator("#real-passphrase").wait_for()
    page.locator("#real-passphrase").fill("browser smoke secret 123")
    page.get_by_role("button", name="Desbloquear").click()
    expect(page.locator("#real-wallet-links-list")).to_contain_text("Firma de prueba · 0x1")
    assert "Sin verificar control" in page.locator("#real-wallet-proof-state").inner_text()
    page.locator("#real-wallet-provider option").nth(1).wait_for(state="attached")
    page.locator("#real-wallet-provider").select_option(label="Test signer")
    page.locator("#real-wallet-connect").click()
    page.get_by_text("Saldo actualizado.").wait_for()
    page.evaluate("window.walletProbe.providers[2].chain = '0x1'; window.walletProbe.providers[2].emit('chainChanged', '0x1')")
    expect(page.locator("#real-wallet-state")).to_contain_text("Ethereum")
    page.once("dialog", accept_control)
    page.locator("#real-wallet-proof").click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("corresponde a esta cuenta REAL")
    page.locator("#real-wallet-links summary").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Desvincular").click()
    expect(page.locator("#real-wallet-links-list")).to_be_empty()
    assert page.evaluate("JSON.stringify(localStorage)").find("0x1111111111111111111111111111111111111111") == -1
    assert page.evaluate("JSON.stringify(localStorage)").find("0x2222222222222222222222222222222222222222") == -1
    assert not any("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in item for item in http_requests)
    assert not any("0x1111111111111111111111111111111111111111" in item for item in http_requests)
    assert set(page.evaluate("window.walletProbe.calls.map(item => item.method)")) == {
        "eth_requestAccounts", "eth_accounts", "eth_chainId", "eth_getBalance", "eth_blockNumber",
        "eth_getBlockByNumber", "eth_call", "personal_sign"
    }
    assert not errors, errors
    browser.close()
    print("Wallet browser flow passed: discovery, ERC-20, Uniswap quote, masking, changes, lock")
