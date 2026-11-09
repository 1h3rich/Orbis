"""Wallet EOA ficticia: plan → autorización única → hash, rechazo e incertidumbre."""
from tests.browser_network import route_once

import json
import os
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import expect, sync_playwright

from tests.test_mexc_deposit_destination import KEY,SECRET,CONTRACT,ADDRESS
from tests.test_wallet_funding_api import PASSWORD


PROVIDER = r"""
(() => {
  const listeners = {}, calls = [];
  const wallet = {address: WALLET_ADDRESS, chain: '0x38', nonce: 3n, calls, sends: [], transactions:{}, mode: 'ok'};
  const word = value => '0x' + BigInt(value).toString(16).padStart(64,'0');
  wallet.emit = (name,value) => (listeners[name] || []).forEach(fn => fn(value));
  wallet.provider = {
    request: async ({method,params}) => {
      calls.push(method);
      if (method === 'eth_requestAccounts' || method === 'eth_accounts') return [wallet.address];
      if (method === 'eth_chainId') return wallet.chain;
      if (method === 'eth_blockNumber') return '0x1234';
      if (method === 'eth_getBlockByNumber') return {number:'0x1234',hash:'0x'+'ab'.repeat(32),timestamp:wallet.blockAt || '0x'+Math.floor(Date.now()/1000).toString(16)};
      if (method === 'eth_getTransactionByHash') return wallet.transactions[params[0]] || null;
      if (method === 'eth_getTransactionReceipt') {
        const tx = wallet.transactions[params[0]]; if (!tx) return null;
        return {transactionHash:tx.hash,blockHash:tx.blockHash,blockNumber:tx.blockNumber,transactionIndex:'0x0',
          from:tx.from,to:tx.to,type:'0x0',status:'0x1',gasUsed:'0xea60',effectiveGasPrice:tx.gasPrice,
          logs:[{address:tx.to,removed:false,transactionHash:tx.hash,blockHash:tx.blockHash,blockNumber:tx.blockNumber,
            transactionIndex:'0x0',logIndex:'0x1',data:'0x'+tx.input.slice(74),
            topics:['0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef',
              '0x'+tx.from.slice(2).toLowerCase().padStart(64,'0'),'0x'+tx.input.slice(10,74)]} ]};
      }
      if (method === 'eth_getBalance') return '0xde0b6b3a7640000';
      if (method === 'eth_getCode') return params[0].toLowerCase() === wallet.address.toLowerCase() ? '0x' : '0x60006000';
      if (method === 'personal_sign') return window.fixtureSign(params[0]);
      if (method === 'eth_getTransactionCount') return '0x'+wallet.nonce.toString(16);
      if (method === 'eth_gasPrice') return '0x3b9aca00';
      if (method === 'eth_estimateGas') return '0xea60';
      if (method === 'eth_call' && params[0].data === '0x313ce567') return word(18);
      if (method === 'eth_call' && params[0].data.startsWith('0x70a08231')) return word(1000n*10n**18n);
      if (method === 'eth_call' && params[0].data.startsWith('0xa9059cbb')) return word(wallet.mode === 'simulate_false' ? 0 : 1);
      if (method === 'eth_sendTransaction') {
        wallet.sends.push(JSON.parse(JSON.stringify(params[0])));
        if (wallet.mode === 'reject') throw {code:4001,message:'fixture user rejection'};
        wallet.nonce += 1n;
        const hash = '0x' + wallet.sends.length.toString(16).padStart(64,'0');
        wallet.blockAt ||= '0x'+Math.floor(Date.now()/1000).toString(16);
        wallet.transactions[hash] = {...params[0],hash,type:'0x0',input:params[0].data,
          blockNumber:'0x1234',blockHash:'0x'+'ab'.repeat(32),transactionIndex:'0x0'};
        if (wallet.mode === 'unknown') throw {code:-32603,message:'fixture lost reply after acceptance'};
        return '0x' + wallet.sends.length.toString(16).padStart(64,'0');
      }
      throw new Error('Unexpected fixture RPC: '+method);
    },
    on: (name,fn) => { (listeners[name] ||= []).push(fn); },
    removeListener: (name,fn) => { listeners[name] = (listeners[name] || []).filter(item => item !== fn); }
  };
  window.testWallet = wallet;
  window.addEventListener('eip6963:requestProvider',() => {
    window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{detail:{
      info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet BEP-20 de prueba'},provider:wallet.provider
    }}));
  });
})();
"""

INTERNAL = os.environ.get("ORBIS_TEST_FUNDING_INTERNAL") == "1"
BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8799")
OUTPUT = Path(os.environ.get("ORBIS_TEST_FUNDING_DIRECTORY", "/tmp"))


with sync_playwright() as playwright:
    wallet = Account.create()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(BASE + "/") else route.abort())
    page.expose_function("fixtureSign",lambda message:"0x"+Account.sign_message(encode_defunct(hexstr=message),wallet.key).signature.hex())
    page.add_init_script(PROVIDER.replace("WALLET_ADDRESS",json.dumps(wallet.address)))
    errors = []
    page.on("pageerror",lambda error:errors.append(str(error)))
    prompt_hash = {"value":"0x"+"1".zfill(64)}
    page.on("dialog",lambda dialog:dialog.accept(prompt_hash["value"]) if dialog.type == "prompt" else dialog.accept())
    page.goto(BASE + "/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.locator("#real-passphrase-confirm").fill(PASSWORD)
    page.get_by_role("button",name="Crear bóveda",exact=True).click()
    page.locator('#real-new-account [name="name"]').fill("MEXC")
    page.get_by_role("button",name="Crear cuenta",exact=True).click()
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill(KEY)
    page.locator('#real-mexc-form [name="secret_key"]').fill(SECRET)
    page.get_by_role("button",name="Consultar MEXC",exact=True).click()
    page.locator("#real-mexc-link > summary").click()
    page.locator('#real-mexc-link-form [name="confirmation"]').fill("VINCULAR")
    page.get_by_role("button",name="Vincular libro",exact=True).click()
    expect(page.locator("#real-mexc-link-state")).to_contain_text("Vínculo local guardado")
    page.get_by_role("button",name="Conectar wallet",exact=True).click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    page.get_by_role("button",name="Verificar control",exact=True).click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    if INTERNAL:
        source = page.request.post(BASE + "/_fixture/linked-wallet",data={"address":wallet.address})
        assert source.status == 200,source.text()
    page.locator("#real-wallet-token-panel > summary").click()
    page.get_by_role("button",name="USDT · BNB",exact=True).click()
    expect(page.locator("#real-wallet-token-state")).to_contain_text("Saldo observado")
    assert page.locator("#real-wallet-token-contract").input_value().lower() == CONTRACT.lower()
    panel = page.locator("#real-wallet-funding")
    panel.locator("summary").click()
    panel.locator('[name="amount"]').fill("12.345678901234567891")
    panel.locator('[name="deposit_minimum"]').fill("1")
    panel.locator('[name="max_gas"]').fill("0.0001")
    panel.get_by_role("button",name="Preparar ingreso",exact=True).click()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Plan listo")
    assert page.evaluate("window.testWallet.sends.length") == 0
    expect(panel.locator("[data-funding-preview]")).to_contain_text(ADDRESS)
    with page.expect_response("**/wallet-funding/*/hash") as reported:
        panel.get_by_role("button",name="Confirmar en wallet",exact=True).click()
    assert reported.value.status == 200,reported.value.text()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Hash registrado")
    assert page.evaluate("window.testWallet.sends.length") == 1
    tx = page.evaluate("window.testWallet.sends[0]")
    assert tx["chainId"] == "0x38" and tx["value"] == "0x0" and tx["to"].lower() == CONTRACT.lower()
    assert tx["data"] == "0xa9059cbb"+ADDRESS[2:].lower().zfill(64)+hex(12345678901234567891)[2:].zfill(64)
    assert int(tx["gas"],16)*int(tx["gasPrice"],16) <= 100000000000000
    expect(panel.locator("[data-funding-history]")).to_contain_text("pendiente de MEXC")
    panel.screenshot(path="/tmp/orbis-wallet-bnb-funding.png")
    assert ADDRESS not in page.evaluate("JSON.stringify(localStorage)")
    assert tx["from"].lower() not in page.evaluate("JSON.stringify(localStorage)").lower()
    # El recibo existe pero View aún no comunica el abono: no inventar saldo.
    with page.expect_response("**/wallet-funding/*/check") as checked:
        panel.get_by_role("button",name="Comprobar ingreso",exact=True).click()
    assert checked.value.status == 200,checked.value.text()
    assert checked.value.json()["status"] == "PENDING_MEXC"
    expect(panel.locator("[data-funding-state]")).to_contain_text("pendiente de acreditación MEXC")
    for code, label in ((1, "Importe insuficiente"), (11, "Restringido")):
        assert page.request.post(BASE + "/_fixture/deposit",params={"status":code}).ok
        with page.expect_response("**/wallet-funding/*/check") as pending:
            panel.get_by_role("button",name="Comprobar ingreso",exact=True).click()
        assert pending.value.ok and pending.value.json()["mexc_deposit"]["code"] == code
        assert not pending.value.json()["exchange_credited"]
        expect(panel.locator("[data-funding-state]")).to_contain_text("MEXC · " + label)
        expect(panel.locator("[data-funding-history]")).to_contain_text("MEXC · " + label)
        expect(panel.get_by_role("button",name="Contabilizar",exact=True)).to_have_count(0)
    panel.get_by_role("button",name="Consultar envíos").click()
    expect(panel.locator("[data-funding-history]")).to_contain_text("Restringido · consulta")
    assert page.request.post(BASE + "/_fixture/deposit").ok
    with page.expect_response("**/wallet-funding/*/check") as credited:
        panel.get_by_role("button",name="Comprobar ingreso",exact=True).click()
    assert credited.value.status == 200,credited.value.text()
    expect(panel.locator("[data-funding-history]")).to_contain_text("Acreditado · sin contabilizar")
    expect(panel.locator("[data-funding-history]")).not_to_contain_text("Restringido")
    panel.get_by_role("button",name="Contabilizar",exact=True).click()
    panel.locator('[name="origin"]').select_option("NEW_INTERNAL" if INTERNAL else "NEW_CONTRIBUTION")
    if INTERNAL:
        expect(panel.locator('[name="origin"] option[value="NEW_CONTRIBUTION"]')).to_have_count(0)
        expect(panel.locator("[data-funding-booking]")).to_contain_text("87.654321098765432109")
        panel.screenshot(path=OUTPUT / "orbis-wallet-internal-preview.png")
        page.locator("#real-hide").click()
        expect(page.locator("#real-hide")).to_have_text("Mostrar importes")
        # La privacidad reconstruye la vista e invalida la revisión pendiente.
        expect(panel.locator("[data-funding-booking]")).to_have_count(0)
        expect(panel).not_to_contain_text("87.654321098765432109")
        page.locator("#real-hide").click()
        expect(page.locator("#real-hide")).to_have_text("Ocultar importes")
        page.get_by_role("button",name="Verificar control",exact=True).click()
        expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
        panel.locator("summary").click()
        panel.get_by_role("button",name="Contabilizar",exact=True).click()
        panel.locator('[name="origin"]').select_option("NEW_INTERNAL")
        page.set_viewport_size({"width":390,"height":844})
        panel.screenshot(path=OUTPUT / "orbis-wallet-internal-preview-mobile.png")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.set_viewport_size({"width":1440,"height":1000})

    def lose_booking_reply(route):
        response = route.fetch()
        assert response.status == 200 and response.json()["booked"]
        route.abort()

    route_once(page,"**/wallet-funding/*/book",lose_booking_reply)
    panel.get_by_role("button",name="Registrar ingreso",exact=True).click()
    expect(panel.locator("[data-funding-history]")).to_contain_text("Acreditado · contabilizado")
    assert page.evaluate("window.testWallet.sends.length") == 1
    if INTERNAL:
        booked = page.request.get(BASE + "/_fixture/source-book").json()
        assert Decimal(booked["cash"]) == Decimal("87.654321098765432109")
        assert Decimal(booked["contribution"]) == 100 and booked["pairs"] == 1
        # La reconstrucción por privacidad también borra los importes del formulario.
        panel.locator('[name="amount"]').fill("12.345678901234567891")
        panel.locator('[name="deposit_minimum"]').fill("1")
        panel.locator('[name="max_gas"]').fill("0.0001")
    panel.screenshot(path=OUTPUT / "orbis-wallet-bnb-credit.png")
    # Rechazo explícito: la siguiente solicitud no se reintenta sola.
    page.evaluate("window.testWallet.mode = 'reject'")
    panel.get_by_role("button",name="Preparar ingreso",exact=True).click()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Plan listo")
    panel.get_by_role("button",name="Confirmar en wallet",exact=True).click()
    expect(panel.locator("[data-funding-state]")).to_contain_text("wallet comunicó rechazo")
    assert page.evaluate("window.testWallet.sends.length") == 2
    # Plan nuevo explícito tras el rechazo y respuesta perdida después del envío.
    panel.get_by_role("button",name="Nuevo plan",exact=True).click()
    page.evaluate("window.testWallet.mode = 'unknown'")
    panel.get_by_role("button",name="Preparar ingreso",exact=True).click()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Plan listo")
    panel.get_by_role("button",name="Confirmar en wallet",exact=True).click()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Resultado de wallet desconocido")
    assert page.evaluate("window.testWallet.sends.length") == 3
    if INTERNAL:
        assert page.request.get(BASE + "/_fixture/source-book").json()["pairs"] == 1
    expect(panel.locator("[data-funding-send]")).to_be_disabled()
    page.set_viewport_size({"width":390,"height":844})
    panel.screenshot(path=OUTPUT / "orbis-wallet-bnb-funding-mobile.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    # Bloquear/reabrir conserva la solicitud incierta, no solicita otro gasto.
    page.get_by_role("button",name="Bloquear",exact=True).click()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    panel.locator("summary").click()
    expect(panel.locator("[data-funding-history]")).to_contain_text("Resultado desconocido")
    expect(panel.locator("[data-funding-history]")).to_contain_text("Acreditado · contabilizado")
    assert page.evaluate("window.testWallet.sends.length") == 3
    # La referencia se recupera sin reconectar la wallet ni volver a gastar.
    prompt_hash["value"] = "0x"+"3".zfill(64)
    with page.expect_response("**/wallet-funding/*/hash") as recovery:
        panel.get_by_role("button",name="Registrar txId",exact=True).first.click()
    assert recovery.value.status == 200,recovery.value.text()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Referencia registrada")
    expect(panel.locator("[data-funding-history]").get_by_text("Resultado desconocido",exact=False)).to_have_count(0)
    assert page.evaluate("window.testWallet.sends.length") == 3
    # Respuesta HTTP perdida tras guardar la concesión: no llega a pedir gasto.
    if page.locator("#real-mexc-connect").get_attribute("open") is None:
        page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill(KEY)
    page.locator('#real-mexc-form [name="secret_key"]').fill(SECRET)
    page.get_by_role("button",name="Consultar MEXC",exact=True).click()
    page.get_by_role("button",name="Conectar wallet",exact=True).click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    page.get_by_role("button",name="Verificar control",exact=True).click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    if page.locator("#real-wallet-token-panel").get_attribute("open") is None:
        page.locator("#real-wallet-token-panel > summary").click()
    page.get_by_role("button",name="USDT · BNB",exact=True).click()
    expect(page.locator("#real-wallet-token-state")).to_contain_text("Saldo observado")
    if panel.locator("details").get_attribute("open") is None:
        panel.locator("summary").click()
    panel.locator('[name="amount"]').fill("1")
    panel.locator('[name="deposit_minimum"]').fill("1")
    panel.locator('[name="max_gas"]').fill("0.0001")
    panel.get_by_role("button",name="Preparar ingreso",exact=True).click()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Plan listo")

    def lose_claim_reply(route):
        response = route.fetch()
        assert response.status == 200 and response.json()["claim_granted"]
        route.abort()

    route_once(page,"**/wallet-funding/*/claim",lose_claim_reply)
    panel.get_by_role("button",name="Confirmar en wallet",exact=True).click()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Permiso no confirmado")
    expect(panel.locator("[data-funding-send]")).to_be_disabled()
    assert page.evaluate("window.testWallet.sends.length") == 3
    panel.get_by_role("button",name="Preparar ingreso",exact=True).click()
    expect(panel.locator("[data-funding-state]")).to_contain_text("Solicitud ya utilizada")
    assert page.evaluate("window.testWallet.sends.length") == 3
    assert not errors,errors
    browser.close()
    print("Wallet BNB UI:","traslado interno" if INTERNAL else "aportación externa","· plan exacto, concesión única, envío controlado, hash, rechazo, incertidumbre y persistencia OK · sin fondos reales")
