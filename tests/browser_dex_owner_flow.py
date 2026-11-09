"""Wallet owner deploys and configures finite authority on a local EVM."""
from tests.browser_network import route_once
import json,os,httpx
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.test_dex_deployments import PASSWORD
BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
PROVIDER=r"""
(() => {
  const handlers={},provider={request:async({method,params=[]})=>{
    (window.fixtureMethods ||= []).push(method);
    if(method==='eth_accounts'||method==='eth_requestAccounts')return [WALLET];
    if(method==='personal_sign')return window.fixtureSign(params[0]);
    if(method==='eth_sendTransaction' && window.rejectOwnerOnce){window.rejectOwnerOnce=false;throw Object.assign(Error('Rejected'),{code:4001});}
    const result=await window.fixtureRpc(method,params);
    if(method==='eth_sendTransaction'&&window.loseOwnerHashOnce){window.loseOwnerHashOnce=false;throw Error('Lost wallet response');}
    return result;
  },on:(name,fn)=>{(handlers[name]||=[]).push(fn)},removeListener:(name,fn)=>{handlers[name]=(handlers[name]||[]).filter(value=>value!==fn)}};
  window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{
    detail:{info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet EVM local'},provider}
  })));
})();
"""
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[];sent=[]
    page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    def bridge(method,params):
        result=httpx.post(BASE+'/__test/wallet-rpc',json={'fixture_id':output.name,'method':method,'params':params},timeout=10,trust_env=False)
        assert result.is_success,result.text
        value=result.json()['result']
        if method=='eth_sendTransaction':sent.append(value)
        return value
    page.expose_function('fixtureRpc',bridge)
    page.expose_function('fixtureSign',lambda message:'0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('11'*32)).signature.hex())
    page.add_init_script(PROVIDER.replace('WALLET',json.dumps(fixture['wallet'])))
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    page.get_by_role('button',name='Conectar wallet',exact=True).click()
    expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
    page.get_by_role('button',name='Verificar control',exact=True).click()
    expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    panel=page.locator('#real-wallet-dex-bot');panel.locator(':scope > details > summary').click()
    owner=panel.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    owner.get_by_role('button',name='Preparar despliegue',exact=True).click()
    expect(owner.locator('[data-owner-review]')).to_contain_text('Desplegar ejecutor')
    page.evaluate('window.loseOwnerHashOnce=true')
    owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Respuesta de wallet sin confirmar')
    owner.locator('summary').filter(has_text='Operaciones del ejecutor').click()
    operation=owner.locator('[data-owner-operation]').first
    expect(operation.get_by_role('button',name='Comprobar',exact=True)).to_be_enabled()
    operation.get_by_role('button',name='Comprobar',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Contrato comprobado')
    expect(operation.locator('input')).to_be_enabled()
    operation.locator('input').fill(sent[0])
    operation.get_by_role('button',name='Comprobar',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Operación confirmada')
    assert page.request.get(BASE+'/__test/state').json()['sends']==1
    # Source amounts are REAL limit templates, never imported PAPER balances.
    panel.get_by_role('button',name='Consultar PAPER',exact=True).click()
    expect(panel.locator('[name="source"] option')).to_have_count(1)
    for name,value in {'entry_usdt':'12','budget_usdt':'60','exposure_usdt':'25','loss_usdt':'5','wbnb_quota':'1'}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.get_by_role('button',name='Comprobar preparación',exact=True).click()
    expect(panel.locator('[data-dex-bot-preview]')).to_contain_text('MEXC · BNB/USDT')
    owner.locator('[name="owner_operator"]').fill(fixture['operator'])
    owner.get_by_role('button',name='Preparar límites',exact=True).click()
    expect(owner.locator('[data-owner-review]')).to_contain_text('Compras y ventas')
    def lose_hash_report(route):
        response=route.fetch();assert response.ok,response.text();route.abort('failed')
    route_once(page,'**/dex-controls/*/hash',lose_hash_report)
    owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    # Receipt recovery includes both encrypted histories and a full RPC state read.
    expect(owner.locator('[data-owner-history]')).to_contain_text('Configurar límites · Recibo pendiente',timeout=15000)
    pending=owner.locator('[data-owner-operation]').filter(has_text='Configurar límites · Recibo pendiente')
    expect(pending.get_by_role('button',name='Comprobar',exact=True)).to_be_enabled()
    pending.get_by_role('button',name='Comprobar',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Operación confirmada')
    assert len(sent)==2
    # Explicit rejection releases that request. A later, reviewed request can proceed.
    expect(owner.get_by_role('button',name='Autorizar USDT',exact=True)).to_be_enabled()
    owner.get_by_role('button',name='Autorizar USDT',exact=True).click()
    expect(owner.locator('[data-owner-review]')).to_contain_text('60 USDT')
    page.evaluate('window.rejectOwnerOnce=true')
    owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    expect(owner.locator('[data-owner-history]')).to_contain_text('Rechazado en wallet')
    expect(owner.get_by_role('button',name='Autorizar USDT',exact=True)).to_be_enabled()
    for action in ['Autorizar USDT','Autorizar WBNB','Revocar USDT','Preparar STOP','Revocar WBNB']:
        expect(owner.get_by_role('button',name=action,exact=True)).to_be_enabled()
        owner.get_by_role('button',name=action,exact=True).click()
        expect(owner.get_by_role('button',name='Confirmar en wallet',exact=True)).to_be_enabled()
        owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
        expect(owner.locator('[data-owner-status]')).to_contain_text('Operación confirmada')
        expect(owner.get_by_role('button',name='Actualizar ejecutor',exact=True)).to_be_enabled()
    assert len(sent)==7
    expect(owner.locator('[data-owner-current]')).to_contain_text('Detenido')
    expect(owner.locator('[data-owner-current]')).to_contain_text('Permiso USDT 0 USDT · WBNB 0 WBNB')
    expect(owner.get_by_role('button',name='Autorizar USDT',exact=True)).to_be_disabled()
    # If the claim reply is lost, do not call the wallet a second time.
    owner.get_by_role('button',name='Preparar STOP',exact=True).click()
    route_once(page,'**/dex-controls/*/claim',lose_hash_report)
    owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    expect(owner.locator('[data-owner-history]')).to_contain_text('Detener ejecutor · Respuesta pendiente')
    assert len(sent)==7
    owner.locator('summary').filter(has_text='Operaciones del ejecutor').click()
    owner.screenshot(path=str(output/'owner-desktop.png'))
    page.locator('#real-lock').click();page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    page.get_by_role('button',name='Conectar wallet',exact=True).click()
    expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
    page.get_by_role('button',name='Verificar control',exact=True).click()
    expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    panel=page.locator('#real-wallet-dex-bot');panel.locator(':scope > details > summary').click()
    owner=panel.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    expect(owner.locator('[data-owner-history]')).to_contain_text('Detener ejecutor · Respuesta pendiente')
    expect(owner.get_by_role('button',name='Actualizar ejecutor',exact=True)).to_be_enabled()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    owner.screenshot(path=str(output/'owner-mobile.png'))
    assert page.request.get(BASE+'/__test/state').json()['sends']==7
    assert not errors,errors
    (output/'browser-result.json').write_text(json.dumps({'passed':True,'local_evm_sends':len(sent),'activation':False,'errors':errors},indent=2))
    browser.close();print('Owner deployment, hash recovery, finite approvals, wallet rejection, STOP, lost claim and reopen verified on disposable EVM')
