"""Wallet-native gas review → reject → one send → lost response → reopen/recover."""
import json,os,re
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.browser_network import route_once
from tests.test_dex_deployments import PASSWORD
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
PROVIDER=r"""
(() => {
 const provider={request:async({method,params})=>{
  (window.fixtureMethods ||= []).push(method);
  if(method==='eth_accounts'||method==='eth_requestAccounts')return [WALLET];
  if(method==='personal_sign')return window.fixtureSign(params[0]);
  if(method==='eth_sendTransaction'&&window.rejectFunding){window.rejectFunding=false;throw Object.assign(Error('Rejected'),{code:4001});}
  const response=await fetch('/__test/wallet-rpc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fixture_id:FIXTURE,method,params:params||[]})});
  if(!response.ok)throw Error('Fixture wallet RPC failed');return (await response.json()).result;
 },on:()=>{},removeListener:()=>{}};
 window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{
  detail:{info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet EVM local'},provider}
 })));
})();
"""
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    page.expose_function('fixtureSign',lambda message:'0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('11'*32)).signature.hex())
    page.add_init_script(PROVIDER.replace('WALLET',json.dumps(fixture['wallet'])).replace('FIXTURE',json.dumps(fixture['fixture_id'])))
    def unlock():
        page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
        page.get_by_role('button',name='Conectar wallet',exact=True).click()
        expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
        page.get_by_role('button',name='Verificar control',exact=True).click()
        expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
        parent=page.locator('#real-wallet-dex-bot');parent.locator(':scope > details > summary').click()
        return parent
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock()
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    gas=parent.locator('[data-dex-gas]');gas.locator(':scope > details > summary').click()
    expect(gas.locator('[name="dex_gas_binding"]')).to_have_value(fixture['binding_id'])
    def prepare():
        gas.locator('[name="dex_gas_amount"]').fill('0.0005')
        gas.get_by_role('button',name='Preparar gas',exact=True).click()
        expect(gas.locator('[data-gas-review] code')).to_have_text(fixture['operator'])
        expect(gas.locator('[data-gas-send]')).to_be_enabled()
    prepare();page.evaluate('window.rejectFunding=true');gas.locator('[data-gas-send]').click()
    expect(gas.locator('[data-gas-status]')).to_contain_text('rechazado')
    assert page.request.get(BASE+'/__test/state').json()['sends']==0
    prepare()
    route_once(page,'**/dex-gas-funding/*/hash',lambda route:route.abort('failed'))
    gas.locator('[data-gas-send]').click()
    expect(gas.locator('[data-gas-history]')).to_contain_text('Respuesta pendiente')
    expect(gas.locator('[data-gas-prepare]')).to_be_enabled()
    state=page.request.get(BASE+'/__test/state').json()
    assert state['sends']==1 and state['operator_balance']==str(5*10**14)
    # Reload loses the in-memory hash/profile; finalized nonce recovery must
    # not invent the amount or authorize a repeat, then the original hash proves it.
    page.locator('#real-lock').click();parent=unlock();gas=parent.locator('[data-dex-gas]')
    gas.locator(':scope > details > summary').click()
    expect(gas.locator('[data-gas-history]')).to_contain_text('Respuesta pendiente')
    gas.get_by_text('Envíos de gas',exact=True).click()
    gas.get_by_role('button',name='Comprobar envío',exact=True).click()
    expect(gas.locator('[data-gas-history]')).to_contain_text('Nonce consumido')
    # Obtain only the disposable EVM transaction hash, as a wallet explorer would.
    tx=page.request.post(BASE+'/__test/wallet-rpc',data={'fixture_id':fixture['fixture_id'],
        'method':'eth_getBlockByNumber','params':['latest',True]}).json()['result']['transactions'][-1]['hash']
    gas.get_by_role('textbox',name=re.compile('Hash del envío')).fill(tx)
    gas.get_by_role('button',name='Comprobar envío',exact=True).click()
    expect(gas.locator('[data-gas-history]')).to_contain_text('BNB confirmado')
    expect(gas.locator('[data-gas-history]')).to_contain_text('Transferido 0,0005 BNB')
    gas.screenshot(path=str(output/'gas-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    gas.screenshot(path=str(output/'gas-mobile.png'))
    assert page.request.get(BASE+'/__test/state').json()['sends']==1
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and tx not in storage and fixture['operator'] not in storage
    assert not errors,errors
    (output/'gas-result.json').write_text(json.dumps({'passed':True,'wallet_sends':1,'response_lost_recovered':True,
        'rejected_without_send':True,'automatic_execution':False,'errors':errors},indent=2))
    browser.close();print('Native BNB review, rejection, one send, lost response and recovery after lock; exact amount and fee')
