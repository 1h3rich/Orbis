"""Actual UI controls -> two signed receipts -> one financial criterion -> cold UI."""
import json,os
from pathlib import Path
from decimal import Decimal as D
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure
from tests.browser_network import route_once
from tests.test_wallet_swap import PASSWORD

SECONDARY=r"""(()=>{const listeners=new Map();const provider={on:(e,h)=>listeners.set(e,h),removeListener:(e)=>listeners.delete(e),request:async({method,params})=>{
 (window.secondaryMethods ||= []).push(method);if(method==='eth_accounts'||method==='eth_requestAccounts')return[WALLET];if(method==='eth_chainId')return'0x38';if(method==='personal_sign')return window.secondarySign(params[0]);throw Error('Read-only secondary wallet');}};
 window.externalListeners=listeners;window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{detail:{info:{uuid:'22222222-2222-4222-8222-222222222222',name:'Segunda wallet local'},provider}})));})();"""
BASE=os.environ['ORBIS_TEST_BASE_URL'];out=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright()as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];replies=[]
    page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==out.name
    configure(page,fixture);page.expose_function('secondarySign',lambda msg:'0x'+Account.sign_message(encode_defunct(hexstr=msg),bytes.fromhex('22'*32)).signature.hex())
    page.add_init_script(SECONDARY.replace('WALLET',json.dumps(fixture['secondary_wallet'])))
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    (out/'initial-dom.yml').write_text(page.locator('body').aria_snapshot())
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    page.get_by_role('button',name='Conectar wallet',exact=True).click();expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
    if page.locator('#real-account').input_value()!=fixture['account_id']:page.locator('#real-account').select_option(fixture['account_id'])
    page.get_by_role('button',name='Verificar control',exact=True).click();expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    before=page.request.get(BASE+'/__test/book').json()
    page.get_by_role('button',name='Recibos externos',exact=True).click();dialog=page.get_by_role('dialog',name='Recibos externos',exact=True)
    expect(dialog.locator('[data-external-source]')).to_have_count(1)
    dialog.locator('.external-wallets > summary').click();dialog.get_by_role('button',name='Segunda',exact=True).click()
    dialog.get_by_label('Proveedor',exact=True).select_option(label='Segunda wallet local')
    dialog.get_by_role('button',name='Elegir cuenta',exact=True).click();dialog.get_by_role('button',name='Verificar',exact=True).click()
    expect(dialog.locator('[data-external-source]')).to_have_count(2)
    for source in fixture['source_ids']:
        card=dialog.locator('[data-external-source="WALLET_SWAP:'+source+'"]')
        card.get_by_role('button',name='Editar',exact=True).click();expect(card.get_by_role('button',name='Traslado',exact=True)).to_be_visible()
        card.get_by_role('button',name='Traslado',exact=True).click();card.get_by_label('Recibo revisado',exact=True).check()
    dialog.get_by_role('button',name='Ocultar importes',exact=True).click()
    assert dialog.locator('textarea').count()==0 and '0,005'not in dialog.inner_text()
    dialog.get_by_role('button',name='Mostrar importes',exact=True).click()
    # Let a completed private preview arrive after its provider context changed.
    def changed(route):
        response=route.fetch();assert response.status==200,response.text()
        page.evaluate("externalListeners.get('chainChanged')('0x1')")
        route.fulfill(response=response)
    route_once(page,'**/external-effect-groups/*/preview',changed)
    dialog.get_by_role('button',name='Revisar selección',exact=True).click()
    expect(dialog).not_to_be_visible()
    assert page.locator('[data-external-body]').inner_text()=='' and page.evaluate('externalListeners.size')==0
    assert page.request.get(BASE+'/__test/book').json()['events']==before['events']
    page.get_by_role('button',name='Recibos externos',exact=True).click()
    expect(dialog.locator('[data-external-source]')).to_have_count(1)
    dialog.locator('.external-wallets > summary').click();dialog.get_by_role('button',name='Segunda',exact=True).click()
    dialog.get_by_label('Proveedor',exact=True).select_option(label='Segunda wallet local')
    dialog.get_by_role('button',name='Elegir cuenta',exact=True).click();dialog.get_by_role('button',name='Verificar',exact=True).click()
    expect(dialog.locator('[data-external-source]')).to_have_count(2)
    for source in fixture['source_ids']:
        card=dialog.locator('[data-external-source="WALLET_SWAP:'+source+'"]')
        card.get_by_role('button',name='Editar',exact=True).click();expect(card.get_by_role('button',name='Traslado',exact=True)).to_be_visible()
        card.get_by_role('button',name='Traslado',exact=True).click();card.get_by_label('Recibo revisado',exact=True).check()
    dialog.locator('[data-external-body]').evaluate('(el)=>el.scrollTop=0')
    dialog.screenshot(path=str(out/'declarations-desktop.png'));page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');dialog.screenshot(path=str(out/'declarations-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1000})
    with page.expect_response('**/external-effect-groups/*/preview')as response:dialog.get_by_role('button',name='Revisar selección',exact=True).click()
    assert response.value.status==200,response.value.text();review=response.value.json();assert review['events_to_create']==6
    expect(dialog.get_by_role('button',name='Confirmar registro',exact=True)).to_be_enabled()
    assert page.request.get(BASE+'/__test/book').json()['events']==before['events']
    dialog.screenshot(path=str(out/'comparison-desktop.png'))
    def lost(route):
        response=route.fetch();assert response.status==200,response.text();replies.append(response.json());route.abort('failed')
    route_once(page,'**/external-effect-groups/*/confirm',lost)
    dialog.get_by_role('button',name='Confirmar registro',exact=True).click()
    expect(dialog.locator('.economic-saved')).to_have_text('Registro guardado ✓')
    after=page.request.get(BASE+'/__test/book').json();assert after['events']==before['events']+6 and after['acceptances']==after['groups']==1 and after['books']==2
    assert after['originals_preserved']and len(replies)==1
    assert D(after['positions'][fixture['account_id']]['WBNB']['cost'])==D('99.7')
    assert D(after['positions'][fixture['peer_id']]['WBNB']['cost'])==D('0.3')
    dialog.get_by_role('button',name='Cerrar',exact=True).click();assert page.evaluate('externalListeners.size')==0
    assert page.locator('[data-external-body]').inner_text()==''
    page.locator('#real-lock').click();page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    if page.locator('#real-account').input_value()!=fixture['account_id']:page.locator('#real-account').select_option(fixture['account_id'])
    page.get_by_role('button',name='Recibos externos',exact=True).click();expect(dialog.get_by_role('button',name='Ver registro',exact=True)).to_have_count(2)
    dialog.get_by_role('button',name='Ver registro',exact=True).first.click();expect(dialog.locator('.economic-saved')).to_have_text('Registro guardado ✓')
    final=page.request.get(BASE+'/__test/book').json();assert final==after
    assert not page.evaluate('(fixtureMethods||[]).concat(window.secondaryMethods||[]).some(m=>/send|signTransaction|signTypedData/.test(m))')
    storage=page.evaluate('JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])')
    assert fixture['wallet'].lower()not in storage.lower()and fixture['secondary_wallet'].lower()not in storage.lower()
    assert not errors,errors
    (out/'result.json').write_text(json.dumps({'passed':True,'signed_receipts':2,'joint_criterion':True,'lost_reply_recovered':True,'cipher_reopen_without_eoa':True,'originals_preserved':True,'wallet_sends':0,'owner_data_used':False,'errors':errors},indent=2))
    browser.close()
