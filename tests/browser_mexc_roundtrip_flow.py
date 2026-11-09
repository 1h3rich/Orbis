"""Desde una wallet registrada hasta su regreso, un único escenario vivo."""
from tests.browser_network import route_once

import json
from decimal import Decimal
import os
from pathlib import Path
import re

from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import expect,sync_playwright

from tests.mexc_roundtrip_fixture import AMOUNT,CONTRACT,DEPOSIT_HASH
from tests.test_mexc_execution_state import PASSWORD


BASE = os.environ.get("ORBIS_TEST_BASE_URL","http://127.0.0.1:8799")
OUTPUT = Path(os.environ["ORBIS_TEST_ROUNDTRIP_DIRECTORY"])
PROVIDER = r"""
(() => {
  const events={}, wallet={address:WALLET,calls:[],evidence:{},block:null,nonce:3n,chain:'0x38'};
  wallet.setAccounts=values=>{wallet.accounts=values;(events.accountsChanged||[]).forEach(fn=>fn(values));};
  wallet.networkCalls=[];
  wallet.networkReplies=[];
  wallet.setChain=(chain,notify=true)=>{
    wallet.chain=chain;
    if(notify)(events.chainChanged||[]).forEach(fn=>fn(chain));
  };
  function networkReply(mode,resolve,reject){
    if(typeof mode==='number'){reject(Object.assign(new Error('Fixture wallet reply'),{code:mode}));return;}
    if(mode==='event'||mode==='silent')wallet.setChain('0x38',mode==='event');
    resolve(null);
  }
  wallet.resolveNetwork=(index,mode)=>wallet.networkReplies[index](mode);
  const word=n=>'0x'+BigInt(n).toString(16).padStart(64,'0');
  const state=async()=>await (await fetch('/__test/state')).json();
  wallet.addEvidence=value=>{wallet.evidence[value.tx_hash]=value;wallet.block=value.block;};
  const provider={request:async({method,params})=>{
    wallet.calls.push(method);
    if(method==='eth_requestAccounts'||method==='eth_accounts')return wallet.accounts ?? [wallet.address];
    if(method==='eth_chainId')return wallet.chain;
    if(method==='wallet_switchEthereumChain'||method==='wallet_addEthereumChain'){
      wallet.networkCalls.push({method,params});
      const mode=wallet.nextNetworkReply ?? (method==='wallet_addEthereumChain'?'unchanged':'event');
      delete wallet.nextNetworkReply;
      return new Promise((resolve,reject)=>{
        if(mode==='hold')wallet.networkReplies.push(reply=>networkReply(reply,resolve,reject));
        else networkReply(mode,resolve,reject);
      });
    }
    if(method==='personal_sign')return window.fixtureSign(params[0]);
    if(method==='eth_blockNumber')return wallet.block?.number||'0x1234';
    if(method==='eth_getBlockByNumber'){
      const known=Object.values(wallet.evidence).find(item=>item.block.number===params[0]);
      return known?.block||wallet.block||{number:'0x1234',hash:'0x'+'c'.repeat(64),timestamp:'0x'+Math.floor(Date.now()/1000).toString(16)};
    }
    if(method==='eth_getTransactionByHash')return wallet.evidence[params[0]]?.transaction||null;
    if(method==='eth_getTransactionReceipt')return params[0]===wallet.withheldFundingReceipt?null:wallet.evidence[params[0]]?.receipt||null;
    if(method==='eth_getBalance')return '0x'+BigInt((await state()).wallet_bnb_wei).toString(16);
    if(method==='eth_getCode')return params[0].toLowerCase()===wallet.address.toLowerCase()?'0x':'0x60006000';
    if(method==='eth_getTransactionCount')return '0x'+wallet.nonce.toString(16);
    if(method==='eth_gasPrice')return wallet.gasPrice||'0x3b9aca00';
    if(method==='eth_estimateGas')return wallet.gasEstimate||'0xea60';
    if(method==='eth_call'&&params[0].data==='0x313ce567')return word(18);
    if(method==='eth_call'&&params[0].data.startsWith('0x70a08231')){
      const text=(await state()).wallet_usdt, [a,b='']=text.split('.');return word(BigInt(a+b.padEnd(18,'0')));
    }
    if(method==='eth_call'&&params[0].data.startsWith('0xa9059cbb'))return word(1);
    if(method==='eth_sendTransaction'){
      if(wallet.awaitExternalCancellation)throw new Error('Fixture unknown outcome');
      const response=await fetch('/__test/wallet-send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params[0])});
      if(!response.ok)throw new Error('Fixture wallet refused duplicate/invalid send');
      const result=await response.json();wallet.nonce+=1n;wallet.addEvidence(result.evidence);
      if(wallet.loseFundingReply)throw new Error('Fixture lost reply after broadcast');
      return result.hash;
    }
    throw new Error('Unexpected fixture wallet method: '+method);
  },on:(name,fn)=>{(events[name]||=[]).push(fn);},removeListener:(name,fn)=>{events[name]=(events[name]||[]).filter(item=>item!==fn);}};
  window.fixtureWallet=wallet;
  window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{detail:{
    info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet de ciclo completo'},provider
  }})));
})();
"""


def select_bnb(page, wallet_step, wallet_status, manual):
    change = page.get_by_role("button", name="Cambiar a BNB Chain", exact=True)
    add = page.get_by_role("button", name="Añadir BNB Chain", exact=True)
    network = page.locator("#real-wallet-network-state")
    expect(change).to_be_hidden()
    # BNB Testnet también es una red equivocada para el circuito de fondos.
    page.evaluate("window.fixtureWallet.setChain('0x61')")
    expect(wallet_status).to_have_text("Selecciona BNB Chain")
    wallet_step.press("Enter")
    expect(change).to_be_focused()
    page.evaluate("window.fixtureWallet.nextNetworkReply=4001")
    change.click()
    expect(network).to_have_text("Cambio cancelado.")
    expect(add).to_be_hidden()

    page.evaluate("window.fixtureWallet.nextNetworkReply=4902")
    change.click()
    expect(add).to_be_enabled()
    assert [call["method"] for call in page.evaluate("window.fixtureWallet.networkCalls")] == [
        "wallet_switchEthereumChain", "wallet_switchEthereumChain"]
    add.click()
    expect(network).to_have_text("Red añadida. Ahora cambia a BNB Chain.")
    expect(wallet_status).to_have_text("Selecciona BNB Chain")
    requests = page.evaluate("window.fixtureWallet.networkCalls")
    assert requests[-1] == {"method": "wallet_addEthereumChain", "params": [{
        "chainId": "0x38", "chainName": "BNB Smart Chain",
        "nativeCurrency": {"name": "BNB", "symbol": "BNB", "decimals": 18},
        "rpcUrls": ["https://bsc-dataseed.bnbchain.org"], "blockExplorerUrls": ["https://bscscan.com"]}]}
    page.evaluate("window.fixtureWallet.nextNetworkReply='unchanged'")
    change.click()
    expect(network).to_have_text("Red sin confirmar. Comprueba tu wallet.")
    expect(wallet_status).to_have_text("Selecciona BNB Chain")
    if manual:
        page.evaluate("window.fixtureWallet.nextNetworkReply='hold'")
        change.click()
        expect(change).to_be_disabled()
        expect(page.get_by_role("button", name="Verificar control", exact=True)).to_be_disabled()
        expect(wallet_status).to_have_text("Confirma en la wallet")
        page.get_by_role("button", name="Desconectar", exact=True).click()
        expect(network).to_be_hidden()
        page.get_by_role("button", name="Conectar wallet", exact=True).click()
        expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
        page.evaluate("window.fixtureWallet.nextNetworkReply='hold'")
        change.click()
        page.wait_for_function("window.fixtureWallet.networkReplies.length===2")
        page.evaluate("async () => {window.fixtureWallet.resolveNetwork(0,4001); await new Promise(r=>setTimeout(r,0));}")
        expect(change).to_be_disabled()
        expect(network).to_have_text("Confirma en la wallet.")
        page.evaluate("window.fixtureWallet.resolveNetwork(1,'event')")
    else:
        page.evaluate("window.fixtureWallet.nextNetworkReply='silent'")
        change.click()
    expect(change).to_be_hidden()
    expect(wallet_status).to_have_text("Verifica el control")
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    calls, requests = page.evaluate("[window.fixtureWallet.calls,window.fixtureWallet.networkCalls]")
    assert not {"personal_sign", "eth_sendTransaction"}.intersection(calls)
    assert len(requests) == (6 if manual else 5)
    assert all(item["params"] == [{"chainId": "0x38"}] for item in requests if item["method"] == "wallet_switchEthereumChain")


def reconnect_mexc_view(page, status, key, label):
    page.locator("#real-mexc-disconnect").click()
    expect(status).to_have_text("Conecta View")
    page.locator("#real-mexc-connect > summary").click()
    page.locator('#real-mexc-form [name="api_key"]').fill(key)
    page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button",name="Consultar MEXC",exact=True).click()
    expect(status).to_have_text(label)


def manual_trade(page, panel, dialogs, *, reopen=False, partial=False):
    limits = panel.locator("[data-trading-limits]")
    panel.get_by_text("Configurar límites manuales",exact=True).click()
    expect(limits).to_be_visible()
    save = limits.get_by_role("button",name="Guardar límites manuales")
    expect(save).to_be_enabled()
    values = {"base_asset":"BTC", "max_order_quote":"30", "max_daily_turnover_quote":"70",
              "max_exposure_quote":"30", "max_daily_loss_quote":"5",
              "max_fee_bps":"20", "max_slippage_bps":"30"}
    for name,value in values.items():
        limits.locator(f'[name="{name}"]').fill(value)
    payloads=[]
    page.on("request",lambda request:payloads.append(request.post_data_json)
            if request.method=="POST" and request.url.endswith("/mexc-manual-limits") else None)
    def lose_saved_reply(route):
        response = route.fetch()
        assert response.status == 200,response.text()
        route.abort()
    route_once(page,"**/mexc-manual-limits",lose_saved_reply)
    save.click()
    expect(limits.locator("[data-trading-limits-status]")).to_have_class("state error")
    with page.expect_response(lambda response: response.request.method=="POST" and
                              response.url.endswith("/mexc-manual-limits")) as saved:
        save.click()
    assert saved.value.status==200,saved.value.text()
    draft=saved.value.json()
    assert draft["manual_only"] and draft["strategy_version_id"] is None
    assert len(payloads)==2 and payloads[0]==payloads[1]
    expect(panel.locator('[name="draft"]')).to_have_value(draft["id"])
    expect(panel.locator("[data-trading-limits-review]")).to_contain_text("Máx. por orden 30 USDT")
    assert page.request.get(BASE+"/__test/state").json()["orders"] == 0

    activation = "resume" if reopen else "activate"
    label = "Reabrir operativa" if reopen else "Habilitar operativa"
    panel.get_by_role("button",name="Revisar saldo",exact=True).click()
    with page.expect_response(f"**/mexc-policy-drafts/*/{activation}") as active:
        panel.get_by_role("button",name=label,exact=True).click()
    assert active.value.status==200,active.value.text()
    assert active.value.json()["manual_only"] and active.value.json()["mode"]=="MANUAL"
    expect(panel.locator("[data-trading-state]")).to_contain_text("activa")
    assert page.request.get(BASE+"/__test/state").json()["orders"] == 0
    review = panel.get_by_role("button",name="Revisar orden",exact=True)
    review_status = panel.locator("[data-trading-review-status]")
    panel.locator('[name="quantity"]').fill("0.000001")
    panel.locator('[name="price"]').fill("65001")
    with page.expect_response("**/mexc-trading-sessions/*/orders/review") as minimum:
        review.click()
    assert minimum.value.ok and minimum.value.json()["reason"]=="MIN_NOTIONAL"
    expect(review_status).to_contain_text("Mínimo")
    assert page.request.get(BASE+"/__test/state").json()["orders"] == 0
    for side,quantity,price in (("BUY","0.0003","65001"),("SELL","0.000299","65000")):
        panel.locator('[name="side"]').select_option(side)
        maximum_sale = panel.get_by_role("button",name="Máximo vendible",exact=True)
        if side == "BUY":
            expect(maximum_sale).to_be_hidden()
            panel.locator('[name="quantity"]').fill(quantity)
            panel.locator('[name="price"]').fill(price)
            expect(review_status).to_be_hidden()
        else:
            orders_before_max = page.request.get(BASE+"/__test/state").json()["orders"]
            with page.expect_response("**/mexc-trading-sessions/*/orders/review") as calculated:
                maximum_sale.click()
            preview = calculated.value
            assert preview.ok and preview.json()["compatible"],preview.text()
            assert preview.request.post_data_json == {"side":"SELL","size_mode":"MAX_AVAILABLE"}
            assert preview.json()["quantity_base"] == quantity and preview.json()["limit_price_quote"] == price
            assert page.request.get(BASE+"/__test/state").json()["orders"] == orders_before_max
            expect(panel.locator('[name="quantity"]')).to_have_value(quantity)
            expect(panel.locator('[name="price"]')).to_have_value(price)
            expect(review_status).to_contain_text("Fuera de la orden 0.000001 BTC")
        prior_orders = page.request.get(BASE+"/__test/state").json()["orders"]
        with page.expect_response("**/mexc-trading-sessions/*/orders/review") as checked:
            review.click()
        preview = checked.value
        assert preview.ok and preview.headers["cache-control"]=="no-store" and preview.json()["compatible"], preview.text()
        assert preview.request.post_data_json == {"side":side,"quantity_base":quantity,"limit_price_quote":price}
        expect(review_status).to_contain_text("Comisión máx. est.")
        assert page.request.get(BASE+"/__test/state").json()["orders"] == prior_orders
        with page.expect_response("**/mexc-trading-sessions/*/orders") as executed:
            panel.get_by_role("button",name="Enviar orden real",exact=True).click()
        assert executed.value.status==200,executed.value.text()
        assert executed.value.json()["journal_status"]=="RESERVATION_CLOSED",executed.value.text()
        expect(panel.locator('[name="quantity"]')).to_have_value("")
        assert quantity in dialogs[-1] and "MEXC REAL" in dialogs[-1]
    if partial:
        assert page.request.post(BASE+"/__test/leave-order-open?immediate_cancel=true").ok
        panel.locator('[name="side"]').select_option("BUY")
        panel.locator('[name="quantity"]').fill("0.0002")
        panel.locator('[name="price"]').fill("65000")
        with page.expect_response("**/mexc-trading-sessions/*/orders") as cancelled:
            panel.get_by_role("button",name="Enviar orden real",exact=True).click()
        assert cancelled.value.ok and cancelled.value.json()["journal_status"] == "RESERVATION_CLOSED"
        assert cancelled.value.json()["synchronization"]["fill_count"] == 0
        expect(panel.locator("[data-trading-state]")).to_have_text("Cancelada sin ejecución.")
    panel.screenshot(path=str(OUTPUT/"orbis-roundtrip-manual.png"))
    assert page.request.post(BASE+"/__test/leave-order-open").ok
    panel.locator('[name="side"]').select_option("BUY")
    panel.locator('[name="quantity"]').fill("0.0002")
    panel.locator('[name="price"]').fill("65000")
    pending = {}

    def lose_accepted_reply(route):
        response = route.fetch()
        assert response.ok,response.text()
        pending.update(response.json())
        assert pending["synchronization"]["stage"]=="ORDER_OPEN",response.text()
        route.abort()

    route_once(page,"**/mexc-trading-sessions/*/orders",lose_accepted_reply)
    panel.get_by_role("button",name="Enviar orden real",exact=True).click()
    expect(panel.locator("[data-trading-state]")).to_contain_text("Orden pendiente")
    return pending["intent_id"]


with sync_playwright() as p:
    wallet = Account.create()
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":1000})
    errors = []; bot_posts = []; dialogs = []
    withdrawal_requests=[]
    page.on('request',lambda request:withdrawal_requests.append((request.method,request.url.split('/mexc-withdrawals')[-1]))
        if '/mexc-withdrawals' in request.url else None)
    page.on("pageerror",lambda error:errors.append(str(error)))
    page.on("request",lambda req:bot_posts.append(req.post_data_json) if req.method=="POST" and req.url.endswith("/mexc-bots") else None)
    def confirm(dialog):
        dialogs.append(dialog.message)
        dialog.accept(DEPOSIT_HASH) if dialog.type == "prompt" else dialog.accept()
    page.on("dialog",confirm)
    page.expose_function("fixtureSign",lambda message:"0x"+Account.sign_message(encode_defunct(hexstr=message),wallet.key).signature.hex())
    page.add_init_script(PROVIDER.replace("WALLET",json.dumps(wallet.address)))
    setup=page.request.get(BASE+"/__test/state").json()
    manual = setup["manual"]
    renewed_view = os.environ.get("ORBIS_TEST_MEXC_VIEW_ROTATION") == "1"
    renewed_transfers = os.environ.get("ORBIS_TEST_MEXC_TRANSFER_VIEW_ROTATION") == "1"
    operational_renewal = os.environ.get("ORBIS_TEST_MEXC_OPERATIONAL_VIEW_RENEWAL") == "1"
    pending_manual_cancel = manual and not renewed_view
    partial = os.environ.get("ORBIS_TEST_MEXC_PARTIAL") == "1"
    manual_order_posts = 4 if partial else 3
    if manual:
        page.goto(BASE+"/ui/#/real")
    else:
        page.goto(BASE+"/ui/#/spot-paper")
        expect(page.locator("#spot-paper-session")).to_have_value(setup["paper_session"])
        page.get_by_role("button",name="Preparar MEXC REAL").click()
        page.wait_for_url("**/#/real")
        expect(page.get_by_text("Prueba PAPER seleccionada para MEXC.")).to_be_visible()
        assert setup["paper_session"] not in page.url and bot_posts==[]
    page.wait_for_load_state("networkidle")
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    expect(page.locator("#real-account")).to_be_visible(timeout=30000)
    bot = page.locator("#real-mexc-bots")
    if not manual:
        expect(bot.locator('[data-bots-handoff]')).to_contain_text("Bot nativo de prueba")
        expect(bot.locator('[name="paper"]')).to_have_value(setup["paper_session"])
        expect(bot.locator('[name="research"]')).to_have_value(setup["validation"])
        expect(bot.locator('[name="budget"]')).to_have_value("")
        expect(bot.locator('[data-bots-start]')).to_be_disabled()
        expect(bot.locator('[data-bots-form-state]')).to_have_text("View · conectar")
        bot.get_by_role("button",name="Conectar View",exact=True).click()
        expect(page.locator("#real-mexc-connect > summary")).to_be_focused()
        page.locator("#real-mexc-connect > summary").click()
    journey = page.get_by_role("navigation",name="Wallet ↔ MEXC")
    expect(journey.locator(".real-route-steps").get_by_role("button")).to_have_count(5)
    expect(journey.get_by_role("button",name="Ingresos directos",exact=True)).to_be_visible()
    view_status = journey.locator("#real-route-view-status")
    wallet_status = journey.locator("#real-route-status-1")
    funding_status = journey.locator("#real-route-status-2")
    order_status = journey.locator("#real-route-status-3")
    bot_status = journey.locator("#real-route-status-4")
    withdrawal_status = journey.locator("#real-route-status-5")
    view_step = journey.get_by_role("button",name="Clave View",exact=True)
    funding_step = journey.get_by_role("button",name="Ingresar USDT → MEXC",exact=True)
    expect(view_status).to_have_text("Conecta View")
    expect(wallet_status).to_have_text("Conecta la wallet")
    journey.screenshot(path=str(OUTPUT/"mexc-route-desktop.png"))
    wallet_step = journey.get_by_role("button",name="Wallet BNB Chain",exact=True)
    wallet_step.focus()
    wallet_step.press("Enter")
    expect(page.locator("#real-wallet-provider")).to_be_focused()
    trade_summary = page.locator("#real-mexc-trading > details > summary")
    journey.get_by_role("button",name="Orden Spot Compra / venta",exact=True).click()
    expect(trade_summary).to_be_focused()
    assert trade_summary.evaluate("node => node.parentElement.open")
    untouched = page.request.get(BASE+"/__test/state").json()
    assert (untouched["wallet_sends"],untouched["orders"],untouched["withdrawals"]) == (0,0,0)
    assert page.evaluate("window.fixtureWallet.calls") == []
    exchange_id = page.locator("#real-account").input_value()
    if not manual:
        bot.locator('[name="budget"]').fill("30")
    page.locator("#real-add-account > summary").click()
    page.locator('#real-add-account-form [name="name"]').fill("Wallet propia")
    with page.expect_response(lambda response:response.request.method == "POST" and response.url.endswith("/real/accounts")) as created:
        page.get_by_role("button",name="Crear cuenta",exact=True).click()
    source_id = created.value.json()["id"]
    expect(page.locator("#real-account")).to_have_value(source_id)
    if not manual:
        expect(bot.locator('[data-bots-handoff-account]')).to_contain_text("Wallet propia")
        expect(bot.locator('[name="budget"]')).to_have_value("")
        expect(bot.locator('[data-bots-start]')).to_be_disabled()
        assert bot_posts==[]
    page.get_by_role("button",name="Conectar wallet",exact=True).click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    expect(wallet_status).to_have_text("Verifica el control")
    select_bnb(page, wallet_step, wallet_status, manual)
    page.get_by_role("button",name="Verificar control",exact=True).click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    page.locator("#real-wallet-links > summary").click()
    page.locator("#real-wallet-link-alias").fill("Principal")
    page.get_by_role("button",name="Guardar vínculo",exact=True).click()
    expect(page.locator("#real-wallet-links-list")).to_contain_text("Principal")
    page.locator("#real-kind").select_option("CONTRIBUTION")
    page.locator('#real-event-form [name="amount"]').fill("100")
    expect(page.locator('#real-event-form [name="occurred_at"]')).not_to_have_value("")
    page.get_by_role("button",name="Guardar movimiento",exact=True).click()
    expect(page.locator("#real-history")).to_contain_text("CONTRIBUTION")
    page.locator("#real-account").select_option(exchange_id)
    journey.get_by_role("button",name="Clave View",exact=True).click()
    expect(page.locator("#real-mexc-connect > summary")).to_be_focused()
    page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
    page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button",name="Consultar MEXC",exact=True).click()
    expect(page.locator("#real-mexc-state")).to_contain_text("MEXC Spot")
    expect(view_status).to_have_text("Vínculo local")
    expect(order_status).to_have_text("Verifica Trade")
    view_step.click()
    link_summary = page.locator("#real-mexc-link > summary")
    expect(link_summary).to_be_focused()
    expect(page.locator("#real-mexc-link-state")).to_contain_text("Vínculo local guardado")
    link_summary.click()
    page.locator("#real-account").select_option(source_id)
    expect(view_status).to_have_text("Elige la cuenta vinculada")
    expect(order_status).to_have_text("Elige la cuenta vinculada")
    page.locator("#real-account").select_option(exchange_id)
    expect(view_status).to_have_text("Vínculo local")
    if operational_renewal:
        reconnect_mexc_view(page,view_status,"fixture-renewed-key","Usa la clave vinculada")
        view_step.click()
        renew_button = page.get_by_role("button",name="Renovar vínculo View",exact=True)
        expect(renew_button).to_be_visible()
        with page.expect_response("**/mexc-link/renew-view") as renewed:
            renew_button.click()
        assert renewed.value.ok and renewed.value.json()["stopped"],renewed.value.text()
        expect(view_status).to_have_text("Vínculo renovado")
        expect(order_status).to_have_text("STOP activo")
        expect(renew_button).to_be_hidden()
        expect(page.locator("#real-mexc-link-state")).to_have_text("Vínculo View guardado.")
    expect(wallet_status).to_have_text("Verifica el control")
    page.get_by_role("button",name="Verificar control",exact=True).click()
    expect(page.locator("#real-wallet-proof-state")).to_contain_text("Control EOA comprobado")
    expect(wallet_status).to_have_text("Control verificado")
    expect(funding_status).to_have_text("Consulta USDT")
    funding_step.click()
    expect(page.locator("#real-wallet-token-panel > summary")).to_be_focused()
    page.get_by_role("button",name="USDT · BNB",exact=True).click()
    expect(page.locator("#real-wallet-token-state")).to_contain_text("Saldo observado")
    expect(funding_status).to_have_text("Revisa importe y destino")
    assert page.locator("#real-wallet-token-contract").input_value().lower() == CONTRACT
    funding = page.locator("#real-wallet-funding")
    journey.get_by_role("button",name="Ingresar USDT → MEXC",exact=True).click()
    expect(funding.locator("summary").first).to_be_focused()
    funding.locator('[name="amount"]').fill(AMOUNT)
    funding.locator('[name="max_gas"]').fill("0.0001")
    minimum = funding.get_by_role("textbox", name="Mínimo en MEXC · USDT")
    minimum.fill(str(Decimal(AMOUNT) + Decimal("0.000000000000000001")))
    calls = page.evaluate("window.fixtureWallet.calls.length")
    funding.get_by_role("button",name="Preparar ingreso",exact=True).click()
    expect(funding.locator("[data-funding-state]")).to_contain_text("inferior al mínimo")
    expect(funding.get_by_role("button",name="Confirmar en wallet",exact=True)).to_be_hidden()
    assert page.evaluate("window.fixtureWallet.calls.length") == calls
    assert page.request.get(BASE+"/__test/state").json()["wallet_sends"] == 0
    minimum.fill(AMOUNT.replace(".", ","))
    gas = funding.locator('[name="max_gas"]')
    estimate_gas = funding.get_by_role("button",name="Estimar gas",exact=True)
    gas_posts = []
    def collect_funding_post(request):
        if request.method == "POST" and "/wallet-funding" in request.url:
            gas_posts.append(request.url)
    page.on("request",collect_funding_post)
    page.evaluate("window.fixtureWallet.gasEstimate='0xea61'")
    estimate_gas.click()
    expect(gas).to_have_value("0.000072002")
    expect(funding.locator("[data-funding-state]")).to_have_text("Gas estimado · revisa el límite")
    expect(funding.get_by_role("button",name="Confirmar en wallet",exact=True)).to_be_hidden()
    assert not gas_posts
    assert page.request.get(BASE+"/__test/state").json()["wallet_sends"] == 0
    funding.locator('[name="amount"]').fill("40")
    expect(gas).to_have_value("")
    funding.locator('[name="amount"]').fill(AMOUNT.replace(".", ","))
    estimate_gas.click()
    expect(gas).to_have_value("0.000072002")
    page.evaluate("window.fixtureWallet.gasPrice='0x77359400'")
    funding.get_by_role("button",name="Preparar ingreso",exact=True).click()
    expect(funding.locator("[data-funding-state]")).to_contain_text("El gas estimado supera tu límite")
    assert not gas_posts
    expect(funding.get_by_role("button",name="Confirmar en wallet",exact=True)).to_be_hidden()
    page.evaluate("delete window.fixtureWallet.gasPrice; delete window.fixtureWallet.gasEstimate;")
    estimate_gas.click()
    expect(gas).to_have_value("0.000072")
    page.remove_listener("request",collect_funding_post)
    with page.expect_response(lambda reply:reply.url.endswith("/wallet-funding") and reply.request.method=="POST") as plan_ready:
        funding.get_by_role("button",name="Preparar ingreso",exact=True).click()
    expect(funding.locator("[data-funding-state]")).to_contain_text("Plan listo")
    plan = plan_ready.value.json()
    assert plan["minimum_deposit_units"] == "40123456789012345678"
    assert plan["gas_wei"] == "72000000000000" and int(plan["transaction"]["gas"],16) == 72000
    expect(funding.locator("[data-funding-preview]")).to_contain_text("mínimo indicado " + AMOUNT)
    funding_closure = os.environ.get("ORBIS_TEST_FUNDING_CLOSURE") == "1"
    if funding_closure:
        from datetime import datetime
        from tests.test_wallet_funding_api import signed_funding_close

        plan = plan_ready.value.json()
        page.evaluate("window.fixtureWallet.awaitExternalCancellation=true")
        funding.get_by_role("button",name="Confirmar en wallet",exact=True).click()
        expect(funding.locator("[data-funding-state]")).to_contain_text("Resultado de wallet desconocido")
        proof = signed_funding_close(plan,wallet,datetime.fromisoformat(plan["created_at"]),kind="CANCELLED",typed=True)
        assert page.request.post(BASE+"/__test/wallet-cancel-funding",data=proof).ok
        with page.expect_response("**/wallet-funding/*/discover") as result:
            funding.get_by_role("button",name="Buscar en BNB",exact=True).click()
        assert result.value.status==200,result.value.text()
        closure = result.value.json()
        assert closure["status"]=="CHAIN_CLOSED" and closure["chain_close"]=="CANCELLED"
        assert not closure["exchange_credited"] and not closure["booked"]
        expect(funding.locator("[data-funding-history]")).to_contain_text("Cancelado en BNB")
        expect(funding_status).to_have_text("Revisa importe y destino")
        expect(funding.get_by_role("button",name="Contabilizar",exact=True)).to_have_count(0)
        assert page.request.get(BASE+"/__test/state").json()["wallet_sends"]==0
        page.evaluate("() => { window.fixtureWallet.awaitExternalCancellation=false; window.fixtureWallet.nonce+=1n; }")
        funding.get_by_role("button",name="Preparar ingreso",exact=True).click()
        expect(funding.locator("[data-funding-state]")).to_contain_text("Plan listo")
    nonce_recovery = os.environ.get("ORBIS_TEST_FUNDING_NONCE") == "1"
    funding_bnb = os.environ.get("ORBIS_TEST_FUNDING_BNB") == "1"
    if funding_bnb:
        page.evaluate("hash => { window.fixtureWallet.withheldFundingReceipt = hash; }", DEPOSIT_HASH)
    if nonce_recovery:
        page.evaluate("window.fixtureWallet.loseFundingReply=true")
    funding.get_by_role("button",name="Confirmar en wallet",exact=True).click()
    if nonce_recovery:
        expect(funding.locator("[data-funding-state]")).to_contain_text("Resultado de wallet desconocido")
        expect(funding.locator("[data-funding-history]")).to_contain_text("Resultado desconocido")
        expect(funding_status).to_have_text("Busca en BNB")
        assert page.request.get(BASE+"/__test/state").json()["wallet_sends"]==1
        page.get_by_role("button",name="Bloquear",exact=True).click()
        page.locator("#real-passphrase").fill(PASSWORD)
        page.get_by_role("button",name="Desbloquear",exact=True).click()
        expect(page.locator("#real-account")).to_be_visible()
        page.locator("#real-account").select_option(exchange_id)
        expect(funding_status).to_have_text("Busca en BNB")
        expect(view_status).to_have_text("Conecta View")
        with page.expect_response("**/wallet-funding?focus_id=*") as pointed:
            funding_step.click()
        funding_focus = pointed.value
        assert funding_focus.ok and funding_focus.request.method == "GET"
        assert funding_focus.request.headers["x-orbis-passive"] == "1"
        funding_id = funding_focus.url.rsplit("=",1)[1]
        funding_card = funding.locator(f'[data-funding-item="{funding_id}"]')
        expect(funding_card).to_be_focused()
        expect(funding_card).to_have_attribute("aria-current","true")
        assert funding_focus.json()[0]["id"] == funding_id
        assert page.request.get(BASE+"/__test/state").json()["wallet_sends"] == 1
        recover = funding_card.get_by_role("button",name="Buscar en BNB",exact=True)
        expect(recover).to_be_disabled()
        page.get_by_role("button",name="Conectar wallet",exact=True).click()
        expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
        page.get_by_role("button",name="Verificar control",exact=True).click()
        expect(recover).to_be_enabled()
        with page.expect_response("**/wallet-funding/*/discover") as discovered:
            recover.click()
        response = discovered.value
        assert response.status==200,response.text()
        assert response.json()["discovered_tx_hash"]==DEPOSIT_HASH
        expect(funding.locator("[data-funding-state]")).to_contain_text("TxId recuperado")
        expect(funding.locator("[data-funding-history]")).to_contain_text("Hash recibido · pendiente de MEXC")
        assert not response.json()["exchange_credited"]
        expect(funding_status).to_have_text("Comprueba el abono")
        page.locator("#real-mexc-connect > summary").click()
        page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
        page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
        page.get_by_role("button",name="Consultar MEXC",exact=True).click()
        expect(page.locator("#real-mexc-state")).to_contain_text("MEXC Spot")
        prompt_count = len(dialogs)
    else:
        expect(funding.locator("[data-funding-state]")).to_contain_text("Hash registrado")
        expect(funding_status).to_have_text("Comprueba el abono")
    funding.get_by_role("button",name="Comprobar ingreso",exact=True).click()
    if funding_bnb:
        expect(funding.locator("[data-funding-state]")).to_contain_text("recibo no disponible")
        read_count = page.evaluate("window.fixtureWallet.calls.filter(name => name.startsWith('eth_getTransaction')).length")
        with page.expect_response("**/wallet-funding/*/check-bnb") as pending_check:
            funding.get_by_role("button",name="Comprobar en BNB",exact=True).click()
        response = pending_check.value
        assert response.ok and response.json()["status"] == "PENDING_MEXC",response.text()
        assert response.json()["mexc_deposit"]["code"] == 1 and not response.json()["exchange_credited"]
        assert response.request.post_data_json["tx_hash"] == DEPOSIT_HASH
        assert page.evaluate("window.fixtureWallet.calls.filter(name => name.startsWith('eth_getTransaction')).length") == read_count
        expect(funding.locator("[data-funding-state]")).to_contain_text("MEXC · Importe insuficiente")
        expect(funding.locator("[data-funding-state]")).to_contain_text("RPC de ORBIS")
        page.get_by_role("button",name="Bloquear",exact=True).click()
        page.locator("#real-passphrase").fill(PASSWORD)
        page.get_by_role("button",name="Desbloquear",exact=True).click()
        page.locator("#real-account").select_option(exchange_id)
        expect(view_status).to_have_text("Conecta View")
        expect(funding_status).to_have_text("Comprueba el abono")
        funding_step.click()
        expect(funding.locator("[data-funding-history]")).to_contain_text("MEXC · Importe insuficiente")
        expect(funding.get_by_role("button",name="Comprobar en BNB",exact=True)).to_be_disabled()
        page.get_by_role("button",name="Conectar wallet",exact=True).click()
        page.get_by_role("button",name="Verificar control",exact=True).click()
        expect(funding.get_by_role("button",name="Comprobar en BNB",exact=True)).to_be_enabled()
        page.locator("#real-mexc-connect > summary").click()
        page.locator('#real-mexc-form [name="api_key"]').fill("fixture-renewed-key" if renewed_transfers else "fixture-key")
        page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
        page.get_by_role("button",name="Consultar MEXC",exact=True).click()
        expect(view_status).to_have_text("Usa la clave vinculada" if renewed_transfers else "Vínculo local")
    else:
        expect(funding.locator("[data-funding-state]")).to_contain_text("MEXC · Importe insuficiente")
        expect(funding.locator("[data-funding-history]")).to_contain_text("MEXC · Importe insuficiente")
    expect(funding.get_by_role("button",name="Contabilizar",exact=True)).to_have_count(0)
    assert page.request.post(BASE+"/__test/credit").ok
    if funding_bnb:
        read_count = page.evaluate("window.fixtureWallet.calls.filter(name => name.startsWith('eth_getTransaction')).length")
        prompt_count = len(dialogs)
        with page.expect_response("**/wallet-funding/*/check-bnb") as credited:
            funding.get_by_role("button",name="Comprobar en BNB",exact=True).click()
        response = credited.value
        assert response.ok and response.json()["chain_source"] == "ORBIS_RPC_OBSERVED",response.text()
        data = response.request.post_data_json
        assert set(data) == {"address"} and data["address"].lower() == wallet.address.lower()
        assert DEPOSIT_HASH not in response.text() and wallet.address.lower() not in response.text().lower()
        assert page.evaluate("window.fixtureWallet.calls.filter(name => name.startsWith('eth_getTransaction')).length") == read_count
    else:
        funding.get_by_role("button",name="Comprobar ingreso",exact=True).click()
    expect(funding.locator("[data-funding-history]")).to_contain_text("Acreditado · sin contabilizar")
    expect(funding_status).to_have_text("Contabiliza el ingreso")
    if nonce_recovery or funding_bnb:
        assert len(dialogs)==prompt_count and page.request.get(BASE+"/__test/state").json()["wallet_sends"]==1
    funding.get_by_role("button",name="Contabilizar",exact=True).click()
    funding.locator('[name="origin"]').select_option("NEW_INTERNAL")
    with page.expect_response("**/wallet-funding/*/book") as booked:
        funding.get_by_role("button",name="Registrar ingreso",exact=True).click()
    assert booked.value.status == 200,booked.value.text()
    expect(funding.locator("[data-funding-history]")).to_contain_text("Acreditado · contabilizado")
    funding.get_by_role("button",name="Ver recorrido",exact=True).click()
    archived_report = funding.locator("[data-funding-roundtrip-report]")
    expect(archived_report).to_contain_text("Recorrido incompleto")
    expect(archived_report).to_contain_text("Ingreso · Contabilizado")
    expect(archived_report).to_contain_text("Compra Spot · Pendiente")
    expect(archived_report).to_contain_text("Venta Spot · Pendiente")
    expect(archived_report).to_contain_text("Retirada · Por solicitar")
    expect(archived_report.locator("[data-funding-private]")).to_have_text(f"· {AMOUNT} USDT · gas 0.00006 BNB")
    archived_report.get_by_role("button",name="Abrir operativa Spot",exact=True).click()
    expect(page.locator("#real-mexc-trading > details > summary")).to_be_focused()
    assert page.request.get(BASE+"/__test/state").json()["orders"] == 0
    if renewed_transfers:
        expect(view_status).to_have_text("Usa la clave vinculada")
        reconnect_mexc_view(page,view_status,"fixture-key","Vínculo local")
    if nonce_recovery or funding_bnb:
        token_panel = page.locator("#real-wallet-token-panel")
        if not token_panel.evaluate("node => node.open"):
            token_panel.locator("summary").click()
        token_panel.get_by_role("button",name="USDT · BNB",exact=True).click()
        expect(page.locator("#real-wallet-token-state")).to_contain_text("Saldo observado")
    expect(funding_status).to_have_text("Revisa importe y destino")
    headers = {"X-Orbis-Vault-Token":booked.value.request.headers["x-orbis-vault-token"]}
    expect(page.locator("#real-history")).to_contain_text("CASH_TRANSFER_IN")
    if manual:
        page.locator("#real-mexc-trade-verify > summary").click()
    else:
        bot.get_by_role("button",name="Configurar Trade",exact=True).click()
        expect(page.locator("#real-mexc-trade-verify > summary")).to_be_focused()
    page.locator('#real-mexc-trade-form [name="api_key"]').fill("fixture-trade")
    page.locator('#real-mexc-trade-form [name="secret_key"]').fill("fixture-secret")
    page.get_by_role("button",name="Verificar Trade",exact=True).click()
    expect(order_status).to_have_text("STOP activo" if operational_renewal else "Revisa saldo y límites")
    if manual:
        expect(page.locator("#real-mexc-trade-state")).to_contain_text("Trade comprobada")
        journey.get_by_role("button",name="Orden Spot Compra / venta",exact=True).click()
        pending_intent = manual_trade(page,page.locator("#real-mexc-trading"),dialogs,
                                      reopen=operational_renewal,partial=partial)
        assert bot_posts == []
        expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(1)
        expect(page.locator("#real-history").get_by_text("SELL",exact=True)).to_have_count(1)
    else:
        picker = page.locator("#real-account")
        picker.select_option(source_id)
        expect(bot.locator('[data-bots-form-state]')).to_have_text("View · otra cuenta")
        expect(bot.locator('[data-bots-start]')).to_be_disabled()
        expect(bot.get_by_role("button",name="Crear límites")).to_be_hidden()
        bot.get_by_role("button",name="Elegir cuenta",exact=True).click()
        expect(picker).to_be_focused()
        assert bot_posts == []
        picker.select_option(exchange_id)
        expect(wallet_status).to_have_text("Verifica el control")
        page.get_by_role("button",name="Verificar control",exact=True).click()
        expect(wallet_status).to_have_text("Control verificado")
        page.get_by_role("button",name="USDT · BNB",exact=True).click()
        expect(page.locator("#real-wallet-token-state")).to_contain_text("Saldo observado")
        journey.get_by_role("button",name="Bot LAB / PAPER",exact=True).click()
        expect(bot.locator("summary").first).to_be_focused()
        expect(bot.locator('[name="paper"] option')).to_have_count(2)
        expect(bot.locator('[name="paper"]')).to_have_value(setup["paper_session"])
        bot.get_by_role("button",name="Crear límites").click()
        expect(page.locator("#real-mexc-policy-drafts > summary")).to_be_focused()
        limits = page.locator("#real-mexc-policy-form")
        expect(limits).to_be_visible()
        expect(limits.locator('[name="strategy_version_id"]')).to_have_value(setup["version"])
        expect(limits.locator('[name="symbol"]')).to_have_value("BTCUSDT")
        assert bot_posts==[] and page.request.get(BASE+"/__test/state").json()["orders"]==0
        bot.locator('[name="budget"]').fill("20")
        bot.get_by_role("button",name="Activar bot",exact=True).click()
        expect(bot.locator("[data-bots-badge]")).to_contain_text("activo")
        expect(bot_status).to_have_text("Bot activo")
        assert len(bot_posts)==1 and bot_posts[0]["paper_session_id"]==setup["paper_session"]
        assert bot_posts[0]["research_validation_id"]==setup["validation"] and bot_posts[0]["entry_budget_quote"]=="20"
        assert "Cuenta REAL: " in dialogs[-1] and "Wallet propia" not in dialogs[-1]
        assert page.request.get(BASE+"/__test/state").json()["orders"] == 0
        if os.environ.get('ORBIS_TEST_EXPECT_CLOCK_PAUSE')=='1':
            expect(bot.locator('[data-bots-list]')).to_contain_text('Último paso: En espera')
            assert page.request.post(BASE+'/__test/arm-clock-shift').ok
            expect(bot.locator('[data-bots-list]')).to_contain_text('Pausado',timeout=10000)
            expect(bot.locator('[data-bots-list]')).to_contain_text('Motivo · Revisar ciclo')
            expect(bot.locator('[data-bots-list]')).not_to_contain_text('Último paso: En espera')
            state=page.request.get(BASE+'/__test/state').json()
            assert state['orders']==0 and all(not worker['running'] for worker in state['workers'])
            assert 'ERROR_NEEDS_REVIEW' in [worker['status'] for worker in state['workers']]
            assert not errors,errors
            browser.close()
            raise SystemExit(0)
        assert page.request.post(BASE+"/__test/entry").json()["poll_result"] == "OBSERVED"
        expect(bot.locator("[data-bots-list]")).to_contain_text("Fills: 1",timeout=15000)
        page.request.post(BASE+"/__test/target")
        expect(bot.locator("[data-bots-list]")).to_contain_text("Fills: 2",timeout=15000)
        expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(1)
        expect(page.locator("#real-history").get_by_text("SELL",exact=True)).to_have_count(1)
        bot.screenshot(path=str(OUTPUT/"orbis-roundtrip-bot.png"))
        # El asiento de vuelta debe refrescar el libro por sí mismo, sin depender
        # de que el panel de bots siga abierto y sondee un cambio de estado.
        bot.locator("summary").click()
    journal = page.locator("#real-mexc-execution")
    if manual and not operational_renewal:
        expect(page.locator("[data-trading-send]")).to_be_disabled()
        expect(order_status).to_have_text("Sincroniza la orden")
        expect(bot_status).to_have_text("Sincroniza la orden")
        if partial:
            pending = page.locator("[data-trading-pending]")
            expect(pending.locator("[data-trading-pending-status]")).to_have_text("Parcial abierta")
            expect(pending.get_by_label("Cantidades de la orden").locator("strong")).to_have_text(
                ["0.0002 BTC", "0.0001 BTC", "0.0001 BTC"])
            expect(pending.get_by_label("Cantidades de la orden")).to_contain_text("Sigue abierto")
            pending.get_by_role("button", name="Revisar orden pendiente", exact=True).click()
            expect(journal.locator(f'.real-exec-card[data-intent="{pending_intent}"]')).to_be_focused()
        page.locator("#real-mexc-disconnect").click()
        expect(order_status).to_have_text("Orden por sincronizar")
        with page.expect_response("**/mexc-pending-orders") as pending_read:
            journey.get_by_role("button",name="Orden Spot Compra / venta",exact=True).click()
        assert pending_read.value.ok and pending_read.value.headers["cache-control"]=="no-store"
        assert pending_read.value.request.headers["x-orbis-passive"]=="1"
        expect(journal.locator(":scope > summary")).to_be_focused()
        order = journal.locator(f'.real-exec-card[data-intent="{pending_intent}"]')
        expect(journal.locator(".real-exec-card")).to_have_count(1)
        sync = order.get_by_role("button",name="Sincronizar orden",exact=True)
        expect(sync).to_be_disabled()
        page.locator("#real-account").select_option(source_id)
        expect(journal).to_contain_text("Sin órdenes pendientes en esta cuenta.")
        expect(journal.locator(".real-exec-card")).to_have_count(0)
        page.locator("#real-account").select_option(exchange_id)
        expect(sync).to_be_disabled()
        def connect_view_from_journal(key):
            journal.get_by_role("button",name="Conectar View",exact=True).click()
            expect(page.locator("#real-mexc-connect > summary")).to_be_focused()
            page.locator('#real-mexc-form [name="api_key"]').fill(key)
            page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
            page.get_by_role("button",name="Consultar MEXC",exact=True).click()
            expect(page.locator("#real-mexc-state")).to_contain_text("MEXC Spot")

        if renewed_view:
            replacement = order.get_by_role("button",name="Recuperar con View renovada",exact=True)
            renewed_path = f"**/intents/{pending_intent}/synchronize-renewed-view"
            connect_view_from_journal("fixture-wrong-view")
            with page.expect_response(renewed_path) as rejected:
                replacement.click()
            assert rejected.value.status == 409 and "inequívocamente" in rejected.value.json()["detail"], rejected.value.text()
            feedback = journal.locator("#real-mexc-execution-feedback")
            expect(feedback).to_contain_text("inequívocamente")
            expect(feedback).to_be_visible()
            page.locator("#real-mexc-disconnect").click()
            connect_view_from_journal("fixture-renewed-key")
            with page.expect_response(renewed_path) as observed:
                replacement.click()
            assert observed.value.ok and observed.value.json()["stage"]=="ORDER_OPEN",observed.value.text()
            assert observed.value.request.post_data_json == {
                "account_id":exchange_id,"confirmation":"RECUPERAR_CON_VIEW_RENOVADA"}
            expect(order.get_by_role("button",name="Cancelar orden",exact=True)).to_have_count(0)
            assert page.request.post(BASE+"/__test/finish-open-order",data={"intent_id":pending_intent}).ok
            with page.expect_response(renewed_path) as recovered:
                replacement.click()
            assert recovered.value.ok and recovered.value.json()["reservation_closed"],recovered.value.text()
            assert recovered.value.json()["external_status"]=="CANCELED" and not recovered.value.json()["orders_enabled"]
            expect(journal).to_contain_text("Sin órdenes pendientes en esta cuenta.")
            expect(view_status).to_have_text("Usa la clave vinculada")
            expect(page.locator("[data-trading-send]")).to_be_disabled()
            page.locator("#real-mexc-disconnect").click()
            expect(view_status).to_have_text("Conecta View")
            page.locator("#real-mexc-connect > summary").click()
            page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
            page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
            page.get_by_role("button",name="Consultar MEXC",exact=True).click()
            expect(view_status).to_have_text("Vínculo local")
        else:
            connect_view_from_journal("fixture-key")
            expect(sync).to_be_enabled()
            with page.expect_response(f"**/intents/{pending_intent}/synchronize") as synchronized:
                sync.click()
            assert synchronized.value.ok and synchronized.value.json()["stage"]=="ORDER_OPEN",synchronized.value.text()
        assert page.request.get(BASE+"/__test/state").json()["orders"]==manual_order_posts
        trade_verification = page.locator("#real-mexc-trade-verify")
        if not trade_verification.evaluate("node => node.open"):
            trade_verification.locator("summary").click()
        page.locator('#real-mexc-trade-form [name="api_key"]').fill("fixture-trade")
        page.locator('#real-mexc-trade-form [name="secret_key"]').fill("fixture-secret")
        page.get_by_role("button",name="Verificar Trade",exact=True).click()
        expect(page.locator("#real-mexc-trade-state")).to_contain_text("Trade comprobada")
        page.get_by_role("button",name="Verificar control",exact=True).click()
        expect(wallet_status).to_have_text("Control verificado")
        token_panel = page.locator("#real-wallet-token-panel")
        if not token_panel.evaluate("node => node.open"):
            token_panel.locator("summary").click()
        token_panel.get_by_role("button",name="USDT · BNB",exact=True).click()
        expect(funding_status).to_have_text("Revisa importe y destino")
    withdrawal = page.locator("#real-wallet-withdrawal")
    if not funding.locator("details").evaluate("node => node.open"):
        funding.locator("summary").click()
    funding.get_by_role("button",name="Ver recorrido",exact=True).click()
    archived_report.get_by_role("button",name="Abrir retirada",exact=True).click()
    expect(withdrawal.locator("summary").first).to_be_focused()
    assert page.request.get(BASE+"/__test/state").json()["withdrawals"] == 0
    withdrawal.get_by_role("button",name="Parar operativa",exact=True).click()
    expect(withdrawal.locator("[data-withdraw-state]")).to_contain_text("Operativa detenida")
    expect(order_status).to_have_text("Sincroniza la orden" if pending_manual_cancel else "STOP activo")
    expect(withdrawal_status).to_have_text(
        "Revisa órdenes o bots pendientes" if pending_manual_cancel else "Revisa destino y saldo")
    withdrawal.locator('[name="amount"]').fill(AMOUNT)
    withdrawal.locator('[name="max_fee"]').fill("0.02")
    if pending_manual_cancel:
        withdrawal.get_by_role("button",name="Calcular máximo",exact=True).click()
        expect(withdrawal.locator("[data-withdraw-state]")).to_contain_text("pendientes de revisión")
        withdrawal.get_by_role("button",name="Preparar retirada",exact=True).click()
        expect(withdrawal.locator("[data-withdraw-state]")).to_contain_text("pendientes de revisión")
        journey.get_by_role("button",name="Bot LAB / PAPER",exact=True).click()
        expect(journal.locator(":scope > summary")).to_be_focused()
        order = journal.locator(f'.real-exec-card[data-intent="{pending_intent}"]')
        cancel = order.get_by_role("button",name="Cancelar orden",exact=True)
        expect(cancel).to_be_enabled()
        with page.expect_response(f"**/intents/{pending_intent}/cancel") as cancelled:
            cancel.click()
        assert cancelled.value.status==200,cancelled.value.text()
        result = cancelled.value.json()
        assert result["request_status"]=="ACKNOWLEDGED" and result["synchronization"]["reservation_closed"]
        assert result["synchronization"]["external_status"] == ("PARTIALLY_CANCELED" if partial else "CANCELED")
        assert result["synchronization"]["booked"] == partial
        expect(journal).to_contain_text("Sin órdenes pendientes en esta cuenta.")
        expect(order_status).to_have_text("STOP activo")
        journal.get_by_role("button",name="Ver historial completo",exact=True).click()
        expect(order).to_contain_text("Resto cancelado · ejecución parcial registrada" if partial else "Cancelada · reserva cerrada")
        if partial:
            expect(order.get_by_label("Cantidades de la orden").locator("strong")).to_have_text(
                ["0.0002 BTC", "0.0001 BTC", "0.0001 BTC"])
            expect(order.get_by_label("Cantidades de la orden")).to_contain_text("Resto cancelado")
            expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(2)
        expect(cancel).to_have_count(0)
        expect(withdrawal_status).to_have_text("Revisa destino y saldo")
        order.screenshot(path=str(OUTPUT/"mexc-cancelled-order.png"))
        journey.get_by_role("button",name="Retirar USDT → wallet",exact=True).click()
    withdrawal.locator('[name="max_fee"]').fill("")
    with page.expect_response("**/mexc-withdrawals/estimate-max") as estimated:
        withdrawal.get_by_role("button",name="Calcular máximo",exact=True).click()
    reply = estimated.value
    assert reply.status == 200 and reply.headers["cache-control"] == "no-store",reply.text()
    assert set(reply.request.post_data_json) == {"address","token_contract","decimals","max_fee"}
    assert reply.request.post_data_json["max_fee"] is None and wallet.address.lower() not in reply.text().lower()
    quote = reply.json()
    state = page.request.get(BASE+"/__test/state").json()
    assert state["withdrawals"] == 0 and Decimal(quote["max_debit"]) == Decimal(state["exchange_usdt"])
    assert Decimal(quote["amount"]) == Decimal(state["exchange_usdt"])-Decimal("0.01")
    expect(withdrawal.locator('[name="amount"]')).to_have_value(quote["amount"])
    expect(withdrawal.locator('[name="max_fee"]')).to_have_value("0.01")
    expect(withdrawal.locator("[data-withdraw-state]")).to_have_text("Máximo calculado · revisa y prepara.")
    expect(withdrawal.locator("[data-withdraw-preview]")).to_contain_text("comisión observada 0.01")
    expect(withdrawal.get_by_role("button",name="Solicitar retirada",exact=True)).not_to_be_visible()
    withdrawal.locator('[name="amount"]').fill(AMOUNT)
    withdrawal.locator('[name="max_fee"]').fill("0.02")
    expect(withdrawal.locator("[data-withdraw-preview]")).to_be_empty()
    with page.expect_response(lambda response:response.request.method=="POST" and
                              response.url.endswith("/mexc-withdrawals")) as prepared:
        withdrawal.get_by_role("button",name="Preparar retirada",exact=True).click()
    assert prepared.value.ok,prepared.value.text()
    archived_plan = prepared.value.json()
    expect(withdrawal.locator("[data-withdraw-state]")).to_contain_text("Plan listo")
    expect(withdrawal_status).to_have_text("Revisa la retirada")
    withdrawal_bnb = os.environ.get("ORBIS_TEST_WITHDRAW_BNB") == "1"
    if withdrawal_bnb:
        page.get_by_role("button",name="Bloquear",exact=True).click()
        expect(page.locator("#real-passphrase")).to_be_visible()
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator("#real-passphrase").fill(PASSWORD)
        page.get_by_role("button",name="Desbloquear",exact=True).click()
        picker = page.locator("#real-account")
        expect(picker).to_be_visible(timeout=30000)
        if picker.input_value() != exchange_id:
            picker.select_option(exchange_id)
        expect(withdrawal_status).to_have_text("Revisa la retirada")
        withdraw_step = journey.get_by_role("button",name="Retirar USDT → wallet",exact=True)
        with page.expect_response("**/mexc-withdrawals?focus_id=*") as pointed:
            withdraw_step.click()
        withdrawal_focus = pointed.value
        assert withdrawal_focus.ok and withdrawal_focus.request.method == "GET"
        assert withdrawal_focus.request.headers["x-orbis-passive"] == "1"
        assert withdrawal_focus.url.endswith("focus_id="+archived_plan["id"])
        card = withdrawal.locator(f'[data-withdraw-item="{archived_plan["id"]}"]')
        expect(card).to_be_focused()
        expect(card).to_have_attribute("aria-current","true")
        review = card.get_by_role("button",name="Revisar plan",exact=True)
        expect(review).to_be_disabled()
        expect(withdrawal.get_by_role("button",name="Solicitar retirada",exact=True)).to_be_hidden()
        view_step.click()
        page.locator('#real-mexc-form [name="api_key"]').fill("fixture-key")
        page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
        page.get_by_role("button",name="Consultar MEXC",exact=True).click()
        expect(view_status).to_have_text("Vínculo local")
        page.get_by_role("button",name="Conectar wallet",exact=True).click()
        expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
        expect(wallet_status).to_have_text("Verifica el control")
        page.get_by_role("button",name="Verificar control",exact=True).click()
        expect(wallet_status).to_have_text("Control verificado")
        expect(funding_status).to_have_text("Consulta USDT")
        funding_step.click()
        expect(page.locator("#real-wallet-token-panel > summary")).to_be_focused()
        page.get_by_role("button",name="USDT · BNB",exact=True).click()
        expect(page.locator("#real-wallet-token-state")).to_contain_text("Saldo observado")
        journey.get_by_role("button",name="Retirar USDT → wallet",exact=True).click()
        with page.expect_response(f"**/mexc-withdrawals/{archived_plan['id']}/review") as reviewed:
            review.click()
        assert reviewed.value.ok and reviewed.value.headers["cache-control"] == "no-store",reviewed.value.text()
        assert reviewed.value.json() == archived_plan
        headers = {"X-Orbis-Vault-Token":reviewed.value.request.headers["x-orbis-vault-token"]}
        assert "secret_key" not in reviewed.value.request.post_data_json
        expect(withdrawal.locator("[data-withdraw-state]")).to_have_text("Plan revisado · confirma para solicitar.")
        expect(withdrawal.locator('[name="amount"]')).to_have_value(AMOUNT)
        expect(withdrawal.locator('[name="max_fee"]')).to_have_value("0.02")
        expect(withdrawal.locator('[name="api_key"]')).to_be_focused()
        expect(withdrawal.locator('[name="secret_key"]')).to_have_value("")
        assert page.request.get(BASE+"/__test/state").json()["withdrawals"] == 0
        assert not any(method=="POST" and path.endswith("/submit") for method,path in withdrawal_requests)
    withdrawal.locator('[name="api_key"]').fill("fixture-withdraw")
    withdrawal.locator('[name="secret_key"]').fill("fixture-withdraw-secret")
    def lose_reply(route):
        response = route.fetch()
        assert response.status == 200,response.text()
        route.abort()
    route_once(page,"**/mexc-withdrawals/*/submit",lose_reply)
    withdrawal.get_by_role("button",name="Solicitar retirada",exact=True).click()
    expect(withdrawal.locator("[data-withdraw-history]")).to_contain_text("Solicitud aceptada")
    expect(withdrawal_status).to_have_text("Comprueba la recepción")
    assert withdrawal.locator('[name="secret_key"]').input_value() == ""
    if renewed_transfers:
        reconnect_mexc_view(page,view_status,"fixture-wrong-view","Usa la clave vinculada")
        with page.expect_response("**/mexc-withdrawals/*/observe") as rejected_view:
            withdrawal.get_by_role("button",name="Comprobar recepción",exact=True).click()
        assert rejected_view.value.status == 409,rejected_view.value.text()
        expect(withdrawal.locator("[data-withdraw-state]")).to_contain_text("inequívocamente")
        reconnect_mexc_view(page,view_status,"fixture-renewed-key","Usa la clave vinculada")
    withdrawal.get_by_role("button",name="Comprobar recepción",exact=True).click()
    expect(withdrawal.locator("[data-withdraw-state]")).to_contain_text("MEXC aún no confirma")
    received = page.request.post(BASE+"/__test/complete").json()
    if withdrawal_bnb:
        withdrawal.get_by_role("button",name="Comprobar recepción",exact=True).click()
        expect(withdrawal.locator("[data-withdraw-state]")).to_contain_text("Recibo aún no disponible")
        wallet_calls = page.evaluate("window.fixtureWallet.calls.filter(method => method.startsWith('eth_getTransaction')).length")
        with page.expect_response("**/mexc-withdrawals/*/receipt-bnb") as server_receipt:
            withdrawal.get_by_role("button",name="Consultar en BNB",exact=True).click()
        result = server_receipt.value
        assert result.ok and result.headers["cache-control"] == "no-store",result.text()
        payload = result.request.post_data_json
        assert set(payload) == {"address"} and payload["address"].lower() == wallet.address.lower()
        assert result.json()["chain_source"] == "ORBIS_RPC_OBSERVED" and not result.json()["booked"]
        assert received["tx_hash"] not in result.text() and wallet.address.lower() not in result.text().lower()
        expect(withdrawal.locator("[data-withdraw-state]")).to_contain_text("RPC de ORBIS")
        assert page.evaluate("window.fixtureWallet.calls.filter(method => method.startsWith('eth_getTransaction')).length") == wallet_calls
    else:
        page.evaluate("value=>window.fixtureWallet.addEvidence(value)",received)
        withdrawal.get_by_role("button",name="Comprobar recepción",exact=True).click()
    expect(withdrawal.locator("[data-withdraw-history]")).to_contain_text("Recibido · sin contabilizar")
    expect(withdrawal_status).to_have_text("Contabiliza la retirada")
    withdrawal.get_by_role("button",name="Contabilizar retirada",exact=True).click()
    form = withdrawal.locator("[data-withdraw-booking]")
    form.locator("select").select_option(label="Traslado interno · Wallet propia")
    stale_history=os.environ.get('ORBIS_TEST_STALE_WITHDRAW_HISTORY')=='1'
    if stale_history:
        page.evaluate('''() => {
          const original=window.fetch;
          const held=window.fixtureStaleWithdrawalHistory={next:true,release:null};
          window.fetch=async function(input,options) {
            const delay=held.next && /\\/mexc-withdrawals$/.test(String(input)) && (!options?.method || options.method==='GET');
            if (delay) held.next=false;
            const response=await original.apply(this,arguments);
            if (!delay) return response;
            const data=await response.clone().json();
            if (data[0].booked!==false) throw new Error('La captura anterior ya estaba contabilizada');
            return await new Promise(resolve=>held.release=()=>resolve(response));
          };
        }''')
        withdrawal.get_by_role('button',name='Consultar retiradas',exact=True).click()
        page.wait_for_function('() => window.fixtureStaleWithdrawalHistory.release !== null')
    pending_reply=os.environ.get('ORBIS_TEST_WITHDRAW_PENDING','')
    assert pending_reply in ('','post','get','both')
    if pending_reply:
        page.evaluate('''mode => {
          const original=window.fetch;
          const probe=window.fixturePendingWithdrawalReply={mode,booked:false,held:{post:0,get:0},aborted:{post:0,get:0}};
          const hold=(phase,signal)=>new Promise((resolve,reject)=>{
            probe.held[phase]++;
            const abort=()=>{probe.aborted[phase]++;reject(new DOMException('Respuesta retenida','AbortError'));};
            if (signal?.aborted) abort(); else signal?.addEventListener('abort',abort,{once:true});
          });
          window.fetch=async function(input,options) {
            const url=String(input),response=await original.apply(this,arguments);
            if (/\\/mexc-withdrawals\\/[^/]+\\/book$/.test(url) && response.ok) {
              const result=await response.clone().json();
              if (!result.booked) throw new Error('No se confirmó el asiento de la prueba');
              probe.booked=true;
              if (['post','both'].includes(mode)) return await hold('post',options?.signal);
            }
            if (probe.booked && /\\/mexc-withdrawals$/.test(url) && !probe.held.get && ['get','both'].includes(mode))
              return await hold('get',options?.signal);
            return response;
          };
        }''',pending_reply)
    else:
        route_once(page,"**/mexc-withdrawals/*/book",lose_reply)
    form.get_by_role("button",name="Registrar retirada",exact=True).click()
    # 3 s de espera del registro y dos lecturas de hasta 2 s; nunca otro POST.
    expect(withdrawal.locator("[data-withdraw-history]")).to_contain_text("Recibido · contabilizado",timeout=8000)
    if pending_reply:
        evidence=page.evaluate('window.fixturePendingWithdrawalReply')
        assert evidence['booked'] is True
        assert evidence['aborted']['post']==(1 if pending_reply in ('post','both') else 0)
        assert evidence['aborted']['get']==(1 if pending_reply in ('get','both') else 0)
    if stale_history:
        page.evaluate('window.fixtureStaleWithdrawalHistory.release()')
        page.wait_for_load_state('networkidle')
        expect(withdrawal.locator('[data-withdraw-history]')).to_contain_text('Recibido · contabilizado')
    expect(page.locator("#real-history")).to_contain_text("CASH_TRANSFER_OUT")
    expect(withdrawal_status).to_have_text("Usa la clave vinculada" if renewed_transfers else "Revisa destino y saldo")
    state = page.request.get(BASE+"/__test/state").json()
    assert sum(method=='POST' and path.endswith('/book') for method,path in withdrawal_requests)==1
    assert (state["wallet_sends"],state["orders"],state["withdrawals"],state["cancellations"]) == (
        (1,manual_order_posts,1,int(pending_manual_cancel)) if manual else (1,2,1,0))
    assert Decimal(state["wallet_usdt"]) == Decimal("99.99")
    assert all(not worker["running"] for worker in state["workers"])
    left = page.request.get(BASE+f"/real/accounts/{source_id}/snapshot",headers=headers).json()
    right = page.request.get(BASE+f"/real/accounts/{exchange_id}/snapshot",headers=headers).json()
    assert Decimal(left["cash"]["USDT"]) == Decimal("99.99") and left["contributions"] == {"USDT":"100"}
    assert Decimal(right["cash"]["USDT"]) == Decimal(state["exchange_usdt"])
    withdrawal.screenshot(path=str(OUTPUT/"orbis-roundtrip-return.png"))
    page.set_viewport_size({"width":390,"height":844})
    journey.screenshot(path=str(OUTPUT/"mexc-route-mobile.png"))
    withdrawal.screenshot(path=str(OUTPUT/"orbis-roundtrip-mobile.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
    storage = page.evaluate("JSON.stringify(localStorage)+JSON.stringify(sessionStorage)")
    assert wallet.address.lower() not in storage.lower() and "fixture-withdraw-secret" not in storage
    page.get_by_role("button",name="Bloquear",exact=True).click()
    page.locator("#real-passphrase").fill(PASSWORD)
    page.get_by_role("button",name="Desbloquear",exact=True).click()
    expect(view_status).to_have_text("Conecta View")
    expect(wallet_status).to_have_text("Conecta la wallet")
    expect(funding_status).to_have_text("Conecta View")
    expect(withdrawal_status).to_have_text("Conecta View")
    if manual:
        trade_summary.click()
        expect(page.locator("[data-trading-send]")).to_be_disabled()
        expect(page.locator("#real-history").get_by_text("BUY",exact=True)).to_have_count(2 if partial else 1)
        if partial:
            journal.locator(":scope > summary").click()
            archived = journal.locator(f'.real-exec-card[data-intent="{pending_intent}"]')
            expect(archived.get_by_label("Cantidades de la orden")).to_contain_text("Resto cancelado")
        expect(page.locator("#real-history").get_by_text("SELL",exact=True)).to_have_count(1)
    else:
        bot.locator("summary").click()
        expect(bot.locator("[data-bots-list]")).to_contain_text("Pausado")
    funding.locator("summary").click()
    expect(funding.locator("[data-funding-history]")).to_contain_text("Acreditado · contabilizado")
    show_report = funding.get_by_role("button",name="Ver recorrido",exact=True)
    expect(show_report).to_be_enabled()
    wallet_requests = page.evaluate("window.fixtureWallet.calls.length")
    with page.expect_response("**/wallet-funding/*/roundtrip") as reviewed:
        show_report.click()
    reply = reviewed.value
    assert reply.ok and reply.headers["cache-control"] == "no-store",reply.text()
    assert reply.request.headers.get("x-orbis-passive") == "1"
    assert reply.request.method == "GET" and reply.request.post_data is None
    assert wallet.address.lower() not in reply.text().lower() and DEPOSIT_HASH not in reply.text()
    assert page.evaluate("window.fixtureWallet.calls.length") == wallet_requests
    expect(wallet_status).to_have_text("Conecta la wallet")
    archived_report = funding.locator("[data-funding-roundtrip-report]")
    expect(archived_report).to_contain_text("Recorrido comprobado")
    expect(archived_report).to_contain_text("Ingreso · Contabilizado")
    origin = "Manual" if manual else "Bot"
    expect(archived_report).to_contain_text(f"Compra Spot · BTC/USDT · {origin}")
    expect(archived_report).to_contain_text(f"Venta Spot · BTC/USDT · {origin}")
    expect(archived_report).to_contain_text("Retirada · Contabilizada")
    expect(archived_report.get_by_role("button")).to_have_count(0)
    amounts = archived_report.locator("[data-funding-private]")
    expect(amounts).to_have_count(4)
    expect(amounts.nth(0)).to_contain_text(f"{AMOUNT} USDT · gas 0.00006 BNB")
    expect(amounts.nth(1)).to_have_text(re.compile(r"· \+\d+(?:\.\d+)? BTC · −\d+(?:\.\d+)? USDT · Comisión 0\.02 USDT"))
    expect(amounts.nth(2)).to_have_text(re.compile(r"· −\d+(?:\.\d+)? BTC · \+\d+(?:\.\d+)? USDT · Comisión 0\.02 USDT"))
    expect(amounts.nth(3)).to_contain_text(f"Neto 40.113456789012345678 USDT · comisión 0.01 USDT · débito {AMOUNT} USDT")
    if not manual:
        page.locator("#real-hide").click()
        expect(page.locator("#real-hide")).to_have_text("Mostrar importes")
        funding.locator("summary").click()
        expect(show_report).to_be_enabled()
        show_report.click()
        expect(archived_report).to_contain_text("Recorrido comprobado")
        expect(amounts).to_have_text(["· Importes ocultos"]*4)
        expect(archived_report).not_to_contain_text(AMOUNT)
        expect(withdrawal.locator("[data-withdraw-estimate]")).to_be_disabled()
    expect(view_status).to_have_text("Conecta View")
    page.evaluate("""window.fixtureWallet.block={number:'0x3456',hash:'0x'+'f'.repeat(64),
        timestamp:'0x'+Math.floor(Date.now()/1000).toString(16)}""")
    page.get_by_role("button",name="Conectar wallet",exact=True).click()
    expect(page.locator("#real-wallet-state")).to_contain_text("Saldo actualizado")
    show_report.click()
    expect(archived_report).to_contain_text("Recorrido comprobado")
    page.evaluate("window.fixtureWallet.setAccounts([])")
    expect(archived_report).to_be_empty()
    expect(show_report).to_be_enabled()
    if funding_bnb:
        expect(funding.locator("[data-funding-history]")).to_contain_text("RPC de ORBIS")
    withdrawal.locator("summary").click()
    expect(withdrawal.locator("[data-withdraw-history]")).to_contain_text("Recibido · contabilizado")
    if withdrawal_bnb:
        expect(withdrawal.locator("[data-withdraw-history]")).to_contain_text("RPC de ORBIS")
    if operational_renewal:
        journey.get_by_role("button",name="Clave View",exact=True).click()
        page.locator('#real-mexc-form [name="api_key"]').fill("fixture-renewed-key")
        page.locator('#real-mexc-form [name="secret_key"]').fill("fixture-secret")
        page.get_by_role("button",name="Consultar MEXC",exact=True).click()
        expect(view_status).to_have_text("Vínculo renovado")
        expect(page.locator("[data-trading-send]")).to_be_disabled()
        journey.get_by_role("button",name="Clave View",exact=True).click()
        expect(page.locator("#real-mexc-link-state")).to_have_text("Vínculo View guardado.")
        expect(page.get_by_role("button",name="Renovar vínculo View",exact=True)).to_be_hidden()
    state = page.request.get(BASE+"/__test/state").json()
    assert (state["wallet_sends"],state["orders"],state["withdrawals"],state["cancellations"]) == (
        (1,manual_order_posts,1,int(pending_manual_cancel)) if manual else (1,2,1,0))
    assert not errors,errors
    browser.close()
    mode = "manual sin LAB" if manual else "bot"
    (OUTPUT/"roundtrip-result.json").write_text(json.dumps({"mode":mode,"passed":True,"errors":errors,"gas_estimate_exact_and_no_writes":True,
        "maximum_sale_verified":manual,
        "wallet_sends":state["wallet_sends"],"orders":state["orders"],"withdrawals":state["withdrawals"],
        "cancellations":state["cancellations"],
        "workers":len(state["workers"]),"bot_posts":len(bot_posts),"external_financial_transactions":0,
        "persisted_transfer_guidance":True,"archived_roundtrip_without_view":True,"archived_roundtrip_without_wallet":True,
        "roundtrip_report_cleared_on_wallet_change":True,"roundtrip_navigation_without_mutation":True,
        **({"partial_remainder_cancelled":True,"partial_journal_focused":True,"closed_without_execution":True} if partial else {}),
        **({"order_review_before_buy_and_sell":True} if manual else {}),
        **({"pending_journal_view_only":True,"pending_journal_account_scope":True,
            "manual_post_reply_lost_without_resend":True} if manual and not operational_renewal else {}),
        **({"renewed_operational_view_completed":True,"saved_view_reconnected_without_trade":True} if operational_renewal else {}),
        **({"renewed_view_booked_both_transfers":True,"withdrawal_wrong_uid_rejected":True} if renewed_transfers else {}),
        **({"renewed_view_closed_original":True,"wrong_uid_rejected":True} if renewed_view else {}),
        **({"funding_bnb_known_then_nonce_after_lock":True,"funding_bnb_without_wallet_receipt":True} if funding_bnb else {}),
        **({"withdrawal_bnb_without_wallet_rpc":True,"legacy_lookup_pending_before_bnb":True,
            "withdrawal_plan_reopened_without_post":True,"withdrawal_focus_passive":True} if withdrawal_bnb else {}),
        **({"funding_focus_passive":True} if nonce_recovery else {}),
        **({"funding_nonce_recovered_after_lock":True,"funding_check_without_txid_prompt":True} if nonce_recovery else {}),
        **({"finalized_cancellation":True,"new_nonce_completed_roundtrip":True} if funding_closure else {})}))
    print(f"Ciclo UI: wallet → MEXC → {mode} BUY/SELL → USDT a la misma wallet. "
          f"Órdenes {state['orders']}, cancelaciones {state['cancellations']}, móvil/reapertura OK · proveedores ficticios")
