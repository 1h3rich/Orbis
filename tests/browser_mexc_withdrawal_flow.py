"""Parar → revisar plan → credencial separada → una petición, no recepción ficticia."""
from tests.browser_network import route_once

import json
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import expect,sync_playwright

from tests.test_wallet_funding_api import PASSWORD
from tests.test_mexc_deposit_destination import KEY,SECRET


PROVIDER = r"""
(() => {
  const events = {},wallet = {address:FIXTURE_WALLET,calls:[]};
  const word = n => '0x'+BigInt(n).toString(16).padStart(64,'0');
  const provider = {request:async({method,params}) => {
    wallet.calls.push(method);
    if (method === 'eth_requestAccounts' || method === 'eth_accounts') return [wallet.address];
    if (method === 'eth_chainId') return '0x38';
    if (method === 'eth_blockNumber') return '0x1234';
    if (method === 'eth_getBlockByNumber') return wallet.evidence ? (params[0] === 'finalized' ? wallet.evidence.finalized : wallet.evidence.block) : {number:'0x1234',hash:'0x'+'ab'.repeat(32),timestamp:'0x'+Math.floor(Date.now()/1000).toString(16)};
    if (method === 'eth_getTransactionByHash') return wallet.evidence && wallet.evidence.transaction;
    if (method === 'eth_getTransactionReceipt') return wallet.evidence && wallet.evidence.receipt;
    if (method === 'eth_getBalance') return '0xde0b6b3a7640000';
    if (method === 'eth_getCode') return params[0].toLowerCase() === wallet.address.toLowerCase() ? '0x' : '0x60006000';
    if (method === 'personal_sign') return window.fixtureSign(params[0]);
    if (method === 'eth_call' && params[0].data === '0x313ce567') return word(18);
    if (method === 'eth_call' && params[0].data.startsWith('0x70a08231')) return word(1000n*10n**18n);
    throw new Error('Unexpected wallet method: '+method);
  },on:(name,fn) => { (events[name] ||= []).push(fn); },removeListener:(name,fn) => {events[name] = (events[name] || []).filter(f => f !== fn);}};
  window.fixtureWallet = wallet;
  window.addEventListener('eip6963:requestProvider',() => window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{detail:{
    info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet de retirada de prueba'},provider
  }})));
})();
"""


with sync_playwright() as p:
    wallet = Account.create()
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors = []
    page.on("pageerror",lambda error:errors.append(str(error)))
    page.on("dialog",lambda dialog:dialog.accept())
    page.expose_function("fixtureSign",lambda message:"0x"+Account.sign_message(encode_defunct(hexstr=message),wallet.key).signature.hex())
    page.add_init_script(PROVIDER.replace("FIXTURE_WALLET",json.dumps(wallet.address)))
    page.goto("http://127.0.0.1:8799/ui/#/real")
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill(KEY)
    page.locator('#real-mexc-form [name="secret_key"]').fill(SECRET)
    page.get_by_role("button",name="Consultar MEXC",exact=True).click()
    page.locator("#real-mexc-link > summary").click()
    page.locator('#real-mexc-link-form [name="confirmation"]').fill("VINCULAR")
    page.get_by_role("button",name="Vincular libro",exact=True).click()
    page.get_by_role("button",name="Conectar wallet",exact=True).click()
    page.get_by_role("button",name="Verificar control",exact=True).click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    page.locator("#real-wallet-token-panel > summary").click()
    page.get_by_role("button",name="USDT · BNB",exact=True).click()
    expect(page.locator("#real-wallet-token-state")).to_contain_text("Saldo observado")
    panel = page.locator("#real-wallet-withdrawal")
    panel.locator("summary").click()
    panel.locator('[name="amount"]').fill("10.123456789012345678")
    panel.locator('[name="max_fee"]').fill("0.02")
    panel.get_by_role("button",name="Preparar retirada",exact=True).click()
    expect(panel.locator("[data-withdraw-state]")).to_contain_text("Detén primero")
    panel.get_by_role("button",name="Parar operativa",exact=True).click()
    expect(panel.locator("[data-withdraw-state]")).to_contain_text("Operativa detenida")
    panel.get_by_role("button",name="Preparar retirada",exact=True).click()
    expect(panel.locator("[data-withdraw-state]")).to_contain_text("Plan listo")
    expect(panel.locator("[data-withdraw-preview]")).to_contain_text("10.143456789012345678")
    assert page.request.get("http://127.0.0.1:8799/_fixture/posts").json()["count"] == 0
    panel.locator('[name="api_key"]').fill("withdraw-key-fixture")
    panel.locator('[name="secret_key"]').fill("withdraw-secret-fixture")
    def lost_reply(route):
        response = route.fetch()
        assert response.status == 200 and response.json()["status"] == "ACKNOWLEDGED",response.text()
        route.abort()
    route_once(page,"**/mexc-withdrawals/*/submit",lost_reply)
    panel.get_by_role("button",name="Solicitar retirada",exact=True).click()
    expect(panel.locator("[data-withdraw-history]")).to_contain_text("Solicitud aceptada · recepción pendiente")
    assert panel.locator('[name="secret_key"]').input_value() == ""
    assert page.request.get("http://127.0.0.1:8799/_fixture/posts").json()["count"] == 1
    assert "eth_sendTransaction" not in page.evaluate("window.fixtureWallet.calls")
    panel.get_by_role("button",name="Comprobar recepción",exact=True).click()
    expect(panel.locator("[data-withdraw-state]")).to_contain_text("MEXC aún no confirma")
    incoming = page.request.post("http://127.0.0.1:8799/_fixture/complete").json()
    page.evaluate("value => window.fixtureWallet.evidence = value",incoming)
    panel.get_by_role("button",name="Comprobar recepción",exact=True).click()
    expect(panel.locator("[data-withdraw-history]")).to_contain_text("Recibido · sin contabilizar")
    expect(panel.locator("[data-withdraw-history]")).to_contain_text("10.113456789012345678")
    panel.get_by_role("button",name="Contabilizar retirada",exact=True).click()
    form = panel.locator("[data-withdraw-booking]")
    form.locator("select").select_option(label="Traslado interno · Wallet")
    saved = {}
    def lost_book_reply(route):
        saved["url"],saved["body"],saved["headers"] = route.request.url,route.request.post_data_json,route.request.headers
        response = route.fetch()
        assert response.status == 200 and response.json()["booked"],response.text()
        route.abort()
    route_once(page,"**/mexc-withdrawals/*/book",lost_book_reply)
    form.get_by_role("button",name="Registrar retirada",exact=True).click()
    expect(panel.locator("[data-withdraw-history]")).to_contain_text("Recibido · contabilizado")
    replay = page.request.post(saved["url"],headers=saved["headers"],data=saved["body"])
    assert replay.status == 200 and replay.json()["booked"],replay.text()
    account = saved["url"].split("/accounts/")[1].split("/")[0]
    target = saved["body"]["target_account_id"]
    left = page.request.get(f"http://127.0.0.1:8799/real/accounts/{account}/snapshot",headers=saved["headers"]).json()
    right = page.request.get(f"http://127.0.0.1:8799/real/accounts/{target}/snapshot",headers=saved["headers"]).json()
    assert left["cash"]["USDT"] == "89.876543210987654322" and right["cash"]["USDT"] == "10.113456789012345678"
    assert Decimal(left["cash_fees"]["USDT"]) == Decimal("0.01") and not left["withdrawals"] and not right["contributions"]
    status = page.request.get(f"http://127.0.0.1:8799/real/accounts/{account}/mexc-execution-status",headers=saved["headers"]).json()
    assert status["stopped"] and not status["withdrawal_pending"] and status["can_review_resume"]
    assert "api_key" not in saved["body"] and "secret_key" not in saved["body"]
    panel.screenshot(path="/tmp/orbis-withdrawal-bnb.png")
    page.set_viewport_size({"width":390,"height":844})
    panel.screenshot(path="/tmp/orbis-withdrawal-bnb-mobile.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
    storage = page.evaluate("JSON.stringify(localStorage)+JSON.stringify(sessionStorage)")
    assert "withdraw-secret-fixture" not in storage and wallet.address.lower() not in storage.lower()
    page.get_by_role("button",name="Ocultar importes",exact=True).click()
    panel.locator("summary").click()
    expect(panel.locator("[data-withdraw-history]")).to_contain_text("Importe oculto")
    expect(panel.locator("[data-withdraw-history]")).not_to_contain_text("10.123456789012345678")
    page.get_by_role("button",name="Bloquear",exact=True).click()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    panel.locator("summary").click()
    expect(panel.locator("[data-withdraw-history]")).to_contain_text("Recibido · contabilizado")
    assert page.request.get("http://127.0.0.1:8799/_fixture/posts").json()["count"] == 1
    assert not errors,errors
    browser.close()
    print("Retirada BSC UI: STOP → un POST → recepción View/RPC → traslado interno neto/comisión; dos respuestas perdidas, replay, privacidad y reapertura OK · sin fondos reales")
