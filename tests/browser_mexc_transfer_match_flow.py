"""UI → confirmación explícita → vínculo cifrado → recarga, sin red MEXC."""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8799"
TX_HASH = "0x" + "ab" * 32
WITHDRAW_HASH = "0x" + "cd" * 32
FAKE_WALLET = r"""
(() => {
  const address = "0x1111111111111111111111111111111111111111";
  const hash = "0x" + "ab".repeat(32);
  const destination = "0x" + "33".repeat(20);
  const contract = "0x" + "44".repeat(20);
  const topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
  const listeners = {};
  const wallet = {address, hash, destination, contract, topic, transferUnits: 25000000n,
    receiptStatus: "0x1", txFrom: address, calls: [], pauseReceipt: false,
    duplicateTransfer: false, nativeTransfer: false, decimalsAvailable: true,
    blockHash: "0x" + "ee".repeat(32), receiptBlockHash: null,
    canonicalHash: null, finalizedHeight: "0x1234", failFinalized: false,
    canonicalUnavailable: false};
  function receipt(txHash) {
    const transfer = {address: wallet.contract,
      topics: [wallet.topic, "0x" + wallet.txFrom.slice(2).padStart(64, "0"),
        "0x" + wallet.destination.slice(2).padStart(64, "0")],
      data: "0x" + wallet.transferUnits.toString(16).padStart(64, "0")};
    return {transactionHash: txHash, from: wallet.txFrom, blockNumber: "0x1230",
      blockHash: wallet.receiptBlockHash || wallet.blockHash,
      status: wallet.receiptStatus, logs: wallet.nativeTransfer ? [] :
        wallet.duplicateTransfer ? [transfer, transfer] : [transfer]};
  }
  wallet.emit = (kind, value) => (listeners[kind] || []).forEach(fn => fn(value));
  wallet.provider = {
    request: async ({method, params}) => {
      wallet.calls.push(method);
      if (method === "eth_requestAccounts" || method === "eth_accounts") return [wallet.address];
      if (method === "eth_chainId") return "0x1";
      if (method === "eth_blockNumber") return "0x1234";
      if (method === "eth_getBlockByNumber") {
        if (params[0] === "0x1234") return {number: "0x1234", hash: wallet.blockHash,
          timestamp: "0x" + Math.floor(Date.now() / 1000).toString(16)};
        if (params[0] === "finalized") {
          if (wallet.failFinalized) throw new Error("finalized unavailable");
          return {number: wallet.finalizedHeight, hash: wallet.blockHash};
        }
        if (params[0] === "0x1230") return wallet.canonicalUnavailable ? null :
          {number: "0x1230", hash: wallet.canonicalHash || wallet.blockHash};
      }
      if (method === "eth_getBalance") return "0x0";
      if (method === "eth_call" && params[0].data === "0x313ce567") {
        if (!wallet.decimalsAvailable) throw new Error("decimals unavailable");
        return "0x" + "6".padStart(64, "0");
      }
      if (method === "eth_getTransactionByHash") return {
        hash: params[0], from: wallet.txFrom, blockNumber: "0x1230", blockHash: wallet.blockHash,
        to: wallet.destination, value: wallet.nativeTransfer ? "0xde0b6b3a7640000" : "0x0", input: "0x"
      };
      if (method === "eth_getTransactionReceipt") {
        if (wallet.pauseReceipt) return new Promise(resolve => {
          wallet.resumeReceipt = () => { wallet.pauseReceipt = false; resolve(receipt(params[0])); };
        });
        return receipt(params[0]);
      }
      throw new Error("Unexpected wallet RPC: " + method);
    },
    on: (kind, fn) => { (listeners[kind] ||= []).push(fn); },
    removeListener: (kind, fn) => { listeners[kind] = (listeners[kind] || []).filter(item => item !== fn); }
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
    page.add_init_script(FAKE_WALLET)
    errors = []
    lookup_posts = []
    refresh_gets = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: lookup_posts.append(request.post_data or "")
            if request.method == "POST" and "/mexc-deposit-tx-lookup" in request.url else None)
    page.on("request", lambda request: refresh_gets.append(request.url)
            if request.method == "GET" and "/exchanges/mexc/transfers?days=7" in request.url else None)
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser transfer match password")
    page.get_by_role("button", name="Desbloquear").click()
    mexc_option = page.locator("#real-account option").filter(has_text="MEXC prueba").first
    expect(mexc_option).to_have_count(1)
    page.locator("#real-account").select_option(value=mexc_option.get_attribute("value"))
    page.locator("#real-mexc-connect summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill("browser-transfer-view-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("browser-transfer-view-secret")
    page.get_by_role("button", name="Consultar MEXC").click()
    page.locator("#real-mexc-transfers-archive summary").click()
    page.get_by_role("button", name="Auditar cuenta").click()
    expect(page.locator("#real-mexc-transfer-audit-result")).to_contain_text("2 MEXC sin vínculo")
    expect(page.locator("#real-mexc-transfer-audit-result")).to_contain_text("0 con cobertura completa, 1 parcial/fuera")
    expect(page.locator("#real-mexc-transfer-audit-result")).to_contain_text("no concilia saldos")
    lookup = page.locator("#real-mexc-deposit-tx-form")
    lookup.locator('input[name="tx_hash"]').fill(TX_HASH)
    lookup.get_by_role("button", name="Buscar ingreso").click()
    expect(page.locator("#real-mexc-deposit-tx-result")).to_contain_text("Ingreso final observado en MEXC")
    expect(page.locator("#real-mexc-deposit-tx-result")).to_contain_text("Sin traslado REAL vinculado")
    assert lookup.locator('input[name="tx_hash"]').input_value() == ""
    assert TX_HASH not in page.locator("#real-mexc-deposit-tx-result").inner_text()
    page.locator("#real-wallet-connect").click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    if not page.locator("#real-wallet-tx-panel").evaluate("element => element.open"):
        page.locator("#real-wallet-tx-panel summary").click()
    wallet_tx = page.locator("#real-wallet-tx-hash")
    page.evaluate("window.testWallet.txFrom = '0x2222222222222222222222222222222222222222'")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("emisor no coincide")
    assert len(lookup_posts) == 1
    page.evaluate("window.testWallet.txFrom = window.testWallet.address; window.testWallet.receiptStatus = '0x0'")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("No hay recibo válido y exitoso")
    assert len(lookup_posts) == 1
    page.evaluate("window.testWallet.receiptStatus = '0x1'")
    page.evaluate("window.testWallet.finalizedHeight = '0x122f'")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("aún no finalizado")
    assert len(lookup_posts) == 1 and len(refresh_gets) == 0
    page.evaluate("window.testWallet.finalizedHeight = '0x1234'; window.testWallet.canonicalHash = '0x' + 'ff'.repeat(32)")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("cadena canónica no coinciden")
    assert len(lookup_posts) == 1 and len(refresh_gets) == 0
    page.evaluate("window.testWallet.canonicalHash = null; window.testWallet.failFinalized = true")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("no verificable por RPC")
    assert len(lookup_posts) == 1 and len(refresh_gets) == 0
    page.evaluate("window.testWallet.failFinalized = false; window.testWallet.canonicalUnavailable = true")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("no verificable por RPC")
    assert len(lookup_posts) == 1 and len(refresh_gets) == 0
    page.evaluate("window.testWallet.canonicalUnavailable = false; window.testWallet.receiptBlockHash = '0x' + 'dd'.repeat(32)")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("cadena canónica no coinciden")
    assert len(lookup_posts) == 1 and len(refresh_gets) == 0
    page.evaluate("window.testWallet.receiptBlockHash = null")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("ingreso final con el mismo txId")
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Destino, importe y contrato ERC-20 coinciden")
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("sin asiento vinculado")
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("no conciliado")
    assert len(refresh_gets) == 1
    assert TX_HASH not in refresh_gets[0]
    assert wallet_tx.input_value() == ""
    assert TX_HASH not in page.locator("#real-wallet-tx-state").inner_text()
    assert TX_HASH not in page.locator("body").inner_text()
    assert TX_HASH not in page.evaluate("JSON.stringify(window.localStorage)")
    assert "eth_sendTransaction" not in page.evaluate("window.testWallet.calls")
    assert len(lookup_posts) == 2
    page.evaluate("window.testWallet.contract = '0x' + '55'.repeat(20)")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Contrato ERC-20 distinto")
    page.evaluate("window.testWallet.contract = '0x' + '44'.repeat(20)")
    assert len(lookup_posts) == 3
    page.evaluate("window.testWallet.transferUnits = 24000000n")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("importe on-chain difiere")
    page.evaluate("window.testWallet.transferUnits = 25000000n; window.testWallet.destination = '0x' + '55'.repeat(20)")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("destino declarado por MEXC no coincide")
    page.evaluate("window.testWallet.destination = '0x' + '33'.repeat(20)")
    assert len(lookup_posts) == 5
    page.evaluate("window.testWallet.duplicateTransfer = true")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("importe ambiguo")
    page.evaluate("window.testWallet.duplicateTransfer = false")
    assert len(lookup_posts) == 6
    page.evaluate("window.testWallet.nativeTransfer = true")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Movimiento nativo incompatible")
    page.evaluate("window.testWallet.nativeTransfer = false")
    assert len(lookup_posts) == 7
    page.evaluate("window.testWallet.pauseReceipt = true")
    wallet_tx.fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    page.wait_for_function("Boolean(window.testWallet.resumeReceipt)")
    page.locator("#real-wallet-disconnect").click()
    page.evaluate("window.testWallet.resumeReceipt()")
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Sin comprobar envíos")
    assert len(lookup_posts) == 7
    page.locator("#real-mexc-transfers-list button").first.click()
    page.get_by_role("button", name="Comparar con cuenta seleccionada").click()
    form = page.locator(".real-mexc-transfer-match")
    expect(form).to_have_count(1)
    expect(page.locator("#real-mexc-transfer-candidates")).to_contain_text("sin conciliación")
    page.locator(".real-mexc-link-candidate summary").click()
    form.locator('input[name="confirmation"]').fill("VINCULAR TRASLADO")
    with page.expect_response("**/real/accounts/*/mexc-transfer-matches") as response:
        form.get_by_role("button", name="Confirmar vínculo").click()
    assert response.value.status == 200, response.value.text()
    expect(page.locator(".real-mexc-link-state")).to_contain_text("Vinculado · no conciliado")
    lookup.locator('input[name="tx_hash"]').fill(TX_HASH)
    lookup.get_by_role("button", name="Buscar ingreso").click()
    expect(page.locator("#real-mexc-deposit-tx-result")).to_contain_text("Asiento REAL vinculado y verificado")
    page.get_by_role("button", name="Auditar cuenta").click()
    expect(page.locator("#real-mexc-transfer-audit-result")).to_contain_text("1 vinculados · 1 MEXC sin vínculo")
    page.locator("#real-wallet-connect").click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    if not page.locator("#real-wallet-tx-panel").evaluate("element => element.open"):
        page.locator("#real-wallet-tx-panel summary").click()
    page.locator("#real-wallet-tx-hash").fill("0x" + "ef" * 32)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("MEXC consultado en los últimos 7 días")
    refreshed_before_withdrawal = len(refresh_gets)
    page.locator("#real-wallet-tx-direction").select_option("withdrawal")
    page.evaluate("window.testWallet.txFrom = '0x2222222222222222222222222222222222222222'; window.testWallet.destination = window.testWallet.address")
    page.locator("#real-wallet-tx-hash").fill("0x" + "ef" * 32)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("MEXC consultado en los últimos 7 días")
    assert len(refresh_gets) == refreshed_before_withdrawal + 1
    def incomplete_coverage(route):
        response = route.fetch()
        payload = response.json()
        payload["coverage"] = "INCOMPLETE"
        route.fulfill(response=response, json=payload)

    page.route("**/exchanges/mexc/transfers?days=7", incomplete_coverage)
    page.locator("#real-wallet-tx-hash").fill("0x" + "ef" * 32)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Actualización MEXC no verificable; búsqueda en capturas archivadas")
    page.unroute("**/exchanges/mexc/transfers?days=7", incomplete_coverage)
    archived_refreshes = len(refresh_gets)
    page.locator("#real-wallet-disconnect").click()
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill("browser transfer match password")
    page.get_by_role("button", name="Desbloquear").click()
    mexc_option = page.locator("#real-account option").filter(has_text="MEXC prueba").first
    expect(mexc_option).to_have_count(1)
    page.locator("#real-account").select_option(value=mexc_option.get_attribute("value"))
    page.locator("#real-mexc-transfers-archive summary").click()
    page.get_by_role("button", name="Auditar cuenta").click()
    expect(page.locator("#real-mexc-transfer-audit-result")).to_contain_text("1 vinculados · 1 MEXC sin vínculo")
    page.locator("#real-mexc-transfers-list button").first.click()
    page.get_by_role("button", name="Comparar con cuenta seleccionada").click()
    expect(page.locator("#real-mexc-transfer-candidates")).to_contain_text("Vinculado · no conciliado")
    expect(page.locator(".real-mexc-transfer-match")).to_have_count(0)
    page.locator("#real-wallet-connect").click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    page.locator("#real-wallet-tx-panel summary").click()
    page.locator("#real-wallet-tx-hash").fill(TX_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("asiento vinculado y verificado")
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("no conciliado")
    page.locator("#real-wallet-tx-direction").select_option("withdrawal")
    page.evaluate("window.testWallet.txFrom = '0x2222222222222222222222222222222222222222'; window.testWallet.destination = window.testWallet.address")
    page.evaluate("window.testWallet.finalizedHeight = '0x122f'")
    withdrawal_refreshes = len(refresh_gets)
    page.locator("#real-wallet-tx-hash").fill(WITHDRAW_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("aún no finalizado")
    assert len(refresh_gets) == withdrawal_refreshes
    page.evaluate("window.testWallet.finalizedHeight = '0x1234'")
    page.locator("#real-wallet-tx-hash").fill(WITHDRAW_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("retiro externo final con mismo txId y destino")
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("importe menos comisión (si es el mismo activo); hipótesis sin verificar")
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("contrato coincide con ficha MEXC, identidad histórica sin verificar")
    page.evaluate("window.testWallet.contract = '0x' + '55'.repeat(20)")
    page.locator("#real-wallet-tx-hash").fill(WITHDRAW_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Contrato ERC-20 distinto")
    page.evaluate("window.testWallet.contract = '0x' + '44'.repeat(20)")
    page.evaluate("window.testWallet.transferUnits = 24000000n")
    page.locator("#real-wallet-tx-hash").fill(WITHDRAW_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("distinto de importe e importe menos comisión")
    page.evaluate("window.testWallet.transferUnits = 25000000n; window.testWallet.duplicateTransfer = true")
    page.locator("#real-wallet-tx-hash").fill(WITHDRAW_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Varios movimientos recibidos; importe ambiguo")
    page.evaluate("window.testWallet.duplicateTransfer = false")
    page.evaluate("window.testWallet.decimalsAvailable = false")
    page.locator("#real-wallet-tx-hash").fill(WITHDRAW_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Token sin decimales verificables")
    page.evaluate("window.testWallet.decimalsAvailable = true; window.testWallet.nativeTransfer = true")
    page.locator("#real-wallet-tx-hash").fill(WITHDRAW_HASH)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Movimiento nativo incompatible")
    page.evaluate("window.testWallet.nativeTransfer = false")
    page.locator("#real-wallet-tx-hash").fill("0x" + "ef" * 32)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Actualización MEXC no verificable; búsqueda en capturas archivadas")
    assert len(refresh_gets) == archived_refreshes
    assert WITHDRAW_HASH not in page.locator("body").inner_text()
    assert "eth_sendTransaction" not in page.evaluate("window.testWallet.calls")
    page.locator("#real-wallet-tx-direction").select_option("deposit")
    page.evaluate("window.testWallet.txFrom = window.testWallet.address; window.testWallet.destination = '0x' + '33'.repeat(20)")
    page.locator("#real-wallet-tx-hash").fill("0x" + "ef" * 32)
    page.locator("#real-wallet-tx-submit").click()
    expect(page.locator("#real-wallet-tx-state")).to_contain_text("Actualización MEXC no verificable; búsqueda en capturas archivadas")
    assert not errors, errors
    print("MEXC transfer match browser flow passed: wallet receipt, explicit link, reload, no trading")
    browser.close()
