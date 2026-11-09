"""Wallet cancels a CREATE → explicit proof → once-only resolution → next CREATE."""
import json,os,httpx
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.test_dex_deployments import PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
PROVIDER=r'''
(() => {
 const provider={request:async({method,params=[]})=>{
  (window.fixtureMethods ||= []).push(method);
  if(method==='eth_accounts'||method==='eth_requestAccounts')return [WALLET];
  if(method==='personal_sign')return window.fixtureSign(params[0]);
  const value=await window.fixtureRpc(method,params);
  if(method==='eth_sendTransaction'&&window.loseOwnerHashOnce){window.loseOwnerHashOnce=false;throw Error('Lost wallet response');}
  return value;
 },on:()=>{},removeListener:()=>{}};
 window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{
 detail:{info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet EVM local'},provider}})));
})();
'''
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name and fixture['cancel_fixture']
    def rpc(method,params):
        response=httpx.post(BASE+'/__test/wallet-rpc',json={'fixture_id':output.name,'method':method,'params':params},timeout=10,trust_env=False)
        assert response.is_success,response.text
        return response.json()['result']
    page.expose_function('fixtureRpc',rpc);page.expose_function('fixtureSign',lambda message:'0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('11'*32)).signature.hex())
    page.add_init_script(PROVIDER.replace('WALLET',json.dumps(fixture['wallet'])))
    def open_owner():
        page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
        page.get_by_role('button',name='Conectar wallet',exact=True).click();expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
        page.get_by_role('button',name='Verificar control',exact=True).click();expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
        panel=page.locator('#real-wallet-dex-bot');panel.locator(':scope > details > summary').click()
        owner=panel.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click();return owner
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');owner=open_owner()
    owner.get_by_role('button',name='Preparar despliegue',exact=True).click()
    expect(owner.locator('[data-owner-review]')).to_contain_text('Desplegar ejecutor')
    page.evaluate('window.loseOwnerHashOnce=true');owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Respuesta de wallet sin confirmar')
    owner.get_by_text('Operaciones del ejecutor',exact=True).click();card=owner.locator('[data-owner-operation]').first
    expect(card).to_contain_text('Respuesta pendiente')
    state=page.request.get(BASE+'/__test/state').json();assert state['sends']==1 and state['replacement_hash']
    expect(card.locator('input')).to_have_value('')
    with page.expect_response('**/dex-deployments/*/discover') as located:
        card.get_by_role('button',name='Revisar reemplazo',exact=True).click()
    assert located.value.status==200 and located.value.json()['discovered_tx_hash']==state['replacement_hash'],located.value.text()
    review=owner.locator('[data-owner-resolution]');expect(review).to_contain_text('Cancelación confirmada')
    assert page.request.get(BASE+'/__test/state').json()['sends']==1
    lost=[]
    def lose(route):
        result=route.fetch();assert result.status==200,result.text();lost.append(result.json());route.abort('failed')
    page.route('**/dex-deployments/*/resolve',lose)
    review.get_by_role('button',name='Confirmar resolución',exact=True).click()
    expect(review).to_contain_text('Resolución sin confirmar')
    expect(review.get_by_role('button',name='Confirmar resolución',exact=True)).not_to_be_visible()
    owner.get_by_role('button',name='Actualizar ejecutor',exact=True).click();expect(card).to_contain_text('Cancelado · Resuelto')
    expect(card.get_by_role('button',name='Comprobar',exact=True)).not_to_be_visible()
    assert len(lost)==1 and lost[0]['outcome']=='CANCELLED' and page.request.get(BASE+'/__test/state').json()['sends']==1
    page.unroute('**/dex-deployments/*/resolve')
    owner.screenshot(path=str(output/'cancel-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+1')
    owner.screenshot(path=str(output/'cancel-mobile.png'))
    page.locator('#real-lock').click();owner=open_owner();owner.get_by_text('Operaciones del ejecutor',exact=True).click()
    expect(owner.locator('[data-owner-history]')).to_contain_text('Cancelado · Resuelto')
    # The consumed old nonce stays reserved; the wallet's next nonce is usable.
    owner.get_by_role('button',name='Preparar despliegue',exact=True).click()
    expect(owner.locator('[data-owner-review]')).to_contain_text('Desplegar ejecutor')
    owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Despliegue confirmado',timeout=20000)
    assert page.request.get(BASE+'/__test/state').json()['sends']==2
    assert not errors,errors
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert state['replacement_hash'] not in storage and PASSWORD not in storage
    (output/'cancel-result.json').write_text(json.dumps({'passed':True,'wallet_sends':2,'cancel_sends':1,'create_sends':1,
        'resolution_sent_no_transaction':True,'lost_resolution_response_recovered':True,'reopen_and_next_nonce':True,'errors':errors},indent=2))
    browser.close();print('Cancelled CREATE, verified same-nonce resolution, lost response recovery, locked reopen and next CREATE')
