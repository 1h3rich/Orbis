"""Generated operator → encrypted file → verified copy → lock → restore; no sends."""
import json,os
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.test_dex_deployments import PASSWORD
from tests.test_dex_operator_drafts import BACKUP_PASSWORD
BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
PROVIDER=r"""
(() => {
 const provider={request:async({method,params})=>{
  (window.fixtureMethods ||= []).push(method);
  if(method==='eth_accounts'||method==='eth_requestAccounts')return [WALLET];
  if(method==='eth_chainId')return '0x38';
  if(method==='eth_getBalance')return '0x1bc16d674ec80000';
  if(method==='eth_blockNumber')return '0x1234';
  if(method==='eth_getBlockByNumber')return {number:'0x1234',hash:'0x'+'c'.repeat(64),timestamp:'0x'+Math.floor(Date.now()/1000).toString(16)};
  if(method==='eth_getCode')return '0x';
  if(method==='personal_sign')return window.fixtureSign(params[0]);
  throw Error('Unexpected wallet method: '+method);
 },on:()=>{},removeListener:()=>{}};
 window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{
  detail:{info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet EVM local'},provider}
 })));
})();
"""
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    page.expose_function('fixtureSign',lambda message:'0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('11'*32)).signature.hex())
    page.add_init_script(PROVIDER.replace('WALLET',json.dumps(fixture['wallet'])))
    def open_operator():
        page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
        page.get_by_role('button',name='Conectar wallet',exact=True).click()
        expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
        page.get_by_role('button',name='Verificar control',exact=True).click()
        expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
        parent=page.locator('#real-wallet-dex-bot');parent.locator(':scope > details > summary').click()
        owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
        operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
        return owner,operator
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');owner,operator=open_operator()
    operator.get_by_role('button',name='Nueva operadora',exact=True).click()
    expect(operator.locator('[data-operator-address]')).to_have_text(__import__('re').compile(r'0x[0-9a-f]{40}'))
    address=operator.locator('[data-operator-address]').inner_text();assert address.lower()!=fixture['wallet'].lower()
    expect(owner.locator('[name="owner_operator"]')).to_have_value('')
    operator.locator('[name="operator_export_password"]').fill(BACKUP_PASSWORD)
    with page.expect_download() as download:
        operator.get_by_role('button',name='Descargar copia',exact=True).click()
    path=output/'encrypted-operator.json';download.value.save_as(path)
    assert json.loads(path.read_text())['schema']=='orbis.dex.operator.backup.v1'
    operator.locator('[name="operator_backup_file"]').set_input_files(path)
    operator.locator('[name="operator_import_password"]').fill('wrong copy password')
    operator.get_by_role('button',name='Comprobar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('contraseña',timeout=10000)
    expect(owner.locator('[name="owner_operator"]')).to_have_value('')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Comprobar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia comprobada',timeout=10000)
    expect(owner.locator('[name="owner_operator"]')).to_have_value(address)
    expect(operator.locator('[name="operator_import_password"]')).to_have_value('')
    operator.screenshot(path=str(output/'operator-desktop.png'))
    page.locator('#real-lock').click();owner,operator=open_operator()
    expect(operator.locator('[data-operator-address]')).to_have_text('')
    operator.locator('[name="operator_backup_file"]').set_input_files(path)
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    expect(owner.locator('[name="owner_operator"]')).to_have_value(address)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    operator.screenshot(path=str(output/'operator-mobile.png'))
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and address not in storage
    operator.get_by_role('button',name='Retirar preparación',exact=True).click()
    expect(operator.locator('[data-operator-address]')).to_have_text('')
    assert not any('send' in method.lower() for method in page.evaluate('window.fixtureMethods'))
    assert page.request.get(BASE+'/__test/state').json()['sends']==0 and not errors,errors
    (output/'operator-result.json').write_text(json.dumps({'passed':True,'wallet_sends':0,'automatic_execution':False,'errors':errors},indent=2))
    browser.close();print('Operator generation, encrypted download, wrong password, verified backup, lock/restore and discard; no wallet sends')
