"""Wallet proof → stored causal binding → lost reply → offline recovery → stop."""
from tests.browser_network import route_once
import json,os
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.test_dex_deployments import PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
PROVIDER=r"""
(() => {
  const handlers={},provider={request:async({method,params})=>{
    (window.fixtureMethods ||= []).push(method);
    if(method==='eth_accounts'||method==='eth_requestAccounts')return [WALLET];
    if(method==='eth_chainId')return '0x38';
    if(method==='eth_getBalance')return '0x1bc16d674ec80000';
    if(method==='eth_blockNumber')return '0x1234';
    if(method==='eth_getBlockByNumber')return {number:'0x1234',hash:'0x'+'c'.repeat(64),timestamp:'0x'+Math.floor(Date.now()/1000).toString(16)};
    if(method==='eth_getCode')return '0x';
    if(method==='personal_sign')return window.fixtureSign(params[0]);
    throw Error('Unexpected wallet method: '+method);
  },on:(name,fn)=>{(handlers[name]||=[]).push(fn)},removeListener:(name,fn)=>{handlers[name]=(handlers[name]||[]).filter(value=>value!==fn)}};
  window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{
    detail:{info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet EVM local'},provider}
  })));
})();
"""
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[];attempts=[]
    page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    page.expose_function('fixtureSign',lambda message:'0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('11'*32)).signature.hex())
    page.add_init_script(PROVIDER.replace('WALLET',json.dumps(fixture['wallet'])))
    page.goto(BASE+'/ui/#/spot-paper')
    expect(page.locator('#spot-paper-session')).to_have_value(fixture['paper_session'])
    page.get_by_role('button',name='Preparar DEX',exact=True).click()
    page.wait_for_url('**/#/real')
    expect(page.get_by_text('Prueba PAPER seleccionada para Uniswap v3.')).to_be_visible()
    assert fixture['paper_session'] not in page.url
    assert not page.evaluate('window.fixtureMethods||[]')
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-wallet-dex-bot')
    select=panel.locator('[name="source"]')
    expect(panel.locator('[data-dex-bot-handoff]')).to_contain_text('PAPER → Uniswap v3')
    expect(select).to_have_value('STRATEGY:'+fixture['paper_session'])
    wrong={'source_kind':'DCA','session_id':fixture['paper_session'],'automatic_execution':False,'preparation_only':True}
    route_once(page,'**/dex-bots/sources/STRATEGY/'+fixture['paper_session'],lambda route:route.fulfill(json=[wrong]))
    panel.get_by_role('button',name='Verificar sesión seleccionada').click()
    expect(panel.locator('[data-dex-bot-status]')).to_have_text('No se pudo verificar la sesión seleccionada.')
    expect(select).to_have_value('')
    expect(panel.locator('[data-dex-bot-review]')).to_be_disabled()
    panel.get_by_role('button',name='Verificar sesión seleccionada').click()
    expect(select).to_have_value('STRATEGY:'+fixture['paper_session'])
    panel.locator('[name="entry_usdt"]').fill('9')
    page.get_by_role('button',name='Conectar wallet',exact=True).click()
    expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
    page.get_by_role('button',name='Verificar control',exact=True).click()
    expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    expect(select).to_have_value('STRATEGY:'+fixture['paper_session'])
    expect(panel.locator('[name="entry_usdt"]')).to_have_value('')
    for name,value in {'entry_usdt':'12','budget_usdt':'60','exposure_usdt':'25','loss_usdt':'5','wbnb_quota':'1'}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.get_by_role('button',name='Comprobar preparación',exact=True).click()
    expect(panel.locator('[data-dex-bot-preview]')).to_contain_text('MEXC · BNB/USDT')
    panel.locator('[data-dex-bot-binding] > summary').click()
    panel.get_by_role('button',name='Consultar ejecutores',exact=True).click()
    expect(panel.locator('[name="deployment_id"]')).to_have_value(fixture['deployment_id'])
    panel.locator('[name="gas_budget_bnb"]').fill('0.002')
    # An explicit policy failure is editable, rather than an unresolvable
    # network-uncertainty state. No grant is created for this rejected request.
    panel.locator('[name="gas_budget_bnb"]').fill('0.000001')
    panel.get_by_role('button',name='Guardar vínculo',exact=True).click()
    expect(panel.locator('[data-dex-bot-status]')).to_contain_text('reserva STOP')
    expect(panel.get_by_role('button',name='Recuperar vínculo',exact=True)).to_be_hidden()
    panel.locator('[name="gas_budget_bnb"]').fill('0.002')
    def lose(route):
        attempts.append(route.request.post_data_json)
        result=route.fetch();assert result.status==200,result.text()
        assert page.request.post(BASE+'/__test/lock-paper').ok
        route.abort('failed')
    route_once(page,'**/dex-bots/bindings',lose)
    panel.get_by_role('button',name='Guardar vínculo',exact=True).click()
    expect(panel.get_by_role('button',name='Recuperar vínculo',exact=True)).to_be_visible()
    expect(panel.get_by_role('button',name='Recuperar vínculo',exact=True)).to_be_enabled()
    before=page.request.get(BASE+'/__test/state').json()['reads']
    with page.expect_request(lambda request:request.method=='POST' and request.url.endswith('/dex-bots/bindings')) as recovered:
        panel.get_by_role('button',name='Recuperar vínculo',exact=True).click()
    assert recovered.value.post_data_json==attempts[0]
    assert attempts[0]['paper_session_id']==fixture['paper_session']
    expect(panel.locator('[data-dex-bot-status]')).to_contain_text('Vínculo recuperado')
    assert page.request.get(BASE+'/__test/state').json()['reads']==before
    history=panel.locator('[data-dex-bot-history]');history.locator('..').locator(':scope > summary').click()
    expect(history).to_contain_text('Vinculado')
    expect(history).to_contain_text('Gas autorizado 0,002 BNB')
    panel.screenshot(path=str(output/'binding-desktop.png'))
    page.locator('#real-lock').click();page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-wallet-dex-bot');panel.locator(':scope > details > summary').click()
    panel.locator('[data-dex-bot-history-panel] > summary').click()
    history=panel.locator('[data-dex-bot-history]')
    expect(history).to_contain_text('Vinculado')
    history.get_by_role('button',name='Retirar vínculo',exact=True).click()
    expect(history).to_contain_text('Vínculo retirado')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    panel.screenshot(path=str(output/'binding-mobile.png'))
    methods=page.evaluate('window.fixtureMethods')
    assert not any('send' in method.lower() for method in methods)
    assert not errors,errors
    (output/'browser-result.json').write_text(json.dumps({'passed':True,'wallet_methods':methods,'activation':False,'errors':errors},indent=2))
    browser.close();print('DEX binding: wallet proof, rejection, atomic save, lost reply, locked-PAPER replay, reopen and local stop; no sends')
