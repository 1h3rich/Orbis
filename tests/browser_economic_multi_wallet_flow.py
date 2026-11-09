"""Full local UI with two discovered EIP-6963 providers and actual signed receipts."""
import json,os
from decimal import Decimal as D,localcontext,Context
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock

BASE=os.environ['ORBIS_TEST_BASE_URL'];out=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
SECONDARY=r"""(()=>{
 const listeners=new Map();
 const provider={on:(event,handler)=>listeners.set(event,handler),removeListener:(event)=>listeners.delete(event),
   request:async({method,params})=>{
     (window.fixtureSecondaryMethods ||= []).push(method);
     if(method==='eth_accounts'||method==='eth_requestAccounts')return[PRIMARY,WALLET];
     if(method==='eth_chainId')return'0x38';
     if(method==='personal_sign')return window.secondaryControlSign(params[0]);
     throw Error('No spending RPC in secondary accounting provider');}};
 window.secondaryProviderListeners=listeners;
 window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{
   detail:{info:{uuid:'22222222-2222-4222-8222-222222222222',name:'Segunda wallet local'},provider}})));
})();"""

with sync_playwright()as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];responses=[];page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    try:
        fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==out.name
        configure(page,fixture)
        def sign(message):
            assert bytes.fromhex(message[2:]).decode().startswith('ORBIS · Verificar control de wallet EOA')
            return '0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('22'*32)).signature.hex()
        page.expose_function('secondaryControlSign',sign)
        page.add_init_script(SECONDARY.replace('WALLET',json.dumps(fixture['secondary_wallet'])).replace('PRIMARY',json.dumps(fixture['wallet'])))
        page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');unlock(page)
        def context():
            if page.locator('#real-account').input_value()!=fixture['account_id']:
                page.locator('#real-account').select_option(fixture['account_id'])
                page.get_by_role('button',name='Verificar control',exact=True).click()
                expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
        context();before=page.request.get(BASE+'/__test/book').json();assert before['events']==4 and before['acceptances']==0
        page.get_by_role('button',name='Revisar cuenta',exact=True).click();dialog=page.get_by_role('dialog',name='Revisar cuenta',exact=True)
        expect(dialog.locator('[data-er-source]')).to_have_count(2)
        dialog.get_by_role('button',name='Conectar',exact=True).click()
        dialog.locator('[data-er-wallet-provider]').select_option(label='Segunda wallet local')
        dialog.get_by_role('button',name='Elegir cuenta',exact=True).click()
        dialog.locator('[data-er-wallet-address]').select_option('1')
        dialog.get_by_role('button',name='Verificar wallet',exact=True).click()
        expect(dialog.locator('[data-er-source]')).to_have_count(3)
        dialog.get_by_role('button',name='Verificar contexto',exact=False).click()
        wallet=dialog.locator('[data-er-source="WALLET_SWAP:'+fixture['wallet_source_id']+'"]')
        wallet.locator('[data-er-trade]').select_option(fixture['manual_trade']);wallet.locator('[data-er-gas="'+fixture['manual_gas']+'"]').check()
        for source in [dialog.locator('[data-er-source^="OPERATOR:"]'),dialog.locator('[data-er-source="WALLET_SWAP:'+fixture['secondary_source_id']+'"]')]:
            if source.locator('[data-er-trade]').count():source.locator('[data-er-trade]').select_option('new')
            if source.locator('[data-er-new-gas]').count():source.locator('[data-er-new-gas]').check()
        dialog.screenshot(path=str(out/'wallets-desktop.png'));page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        dialog.screenshot(path=str(out/'wallets-mobile.png'));page.set_viewport_size({'width':1440,'height':1000})
        with page.expect_response('**/economic-review/preview')as reviewed:
            dialog.get_by_role('button',name='Revisar',exact=True).click()
        assert reviewed.value.status==200,reviewed.value.text();preview=reviewed.value.json()
        request=reviewed.value.request.post_data_json
        by_id={row['source_id']:row for row in request['receipts']}
        assert by_id[fixture['secondary_source_id']]['wallet_address'].lower()==fixture['secondary_wallet'].lower()
        assert by_id[fixture['wallet_source_id']]['wallet_address'].lower()==fixture['wallet'].lower()
        assert len(request['wallets'])==1 and request['wallets'][0]['address'].lower()==fixture['secondary_wallet'].lower()
        assert preview['review']['events_to_create']==5 and preview['review']['checked_receipts']==3
        with localcontext(Context(prec=300)):
            units=sum((D(item['quantity'])for item in fixture['economic_inputs']),D(0))
            cost=sum((D(item['gross_amount'])for item in fixture['economic_inputs']),D(0))
            gas=sum((D(item['gas_wei'])for item in fixture['economic_inputs']),D(0))/10**18
        account=next(item['network']for item in preview['review']['accounts']if item['account_id']==fixture['ledger_id'])
        position=next(item for item in account['positions']if item['asset']=='WBNB')
        assert D(position['quantity'])==units and D(position['cost_basis'])==cost
        assert D(account['cash']['USDT'])==D('100')-cost and D(account['cash']['BNB'])==D('0.01')-gas
        assert page.request.get(BASE+'/__test/book').json()['events']==4
        def lost(route):
            response=route.fetch();assert response.status==200,response.text();responses.append(response.json());route.abort('failed')
        page.route('**/economic-review/confirm',lost);dialog.get_by_role('button',name='Aceptar criterio',exact=True).click()
        expect(dialog.get_by_role('button',name='Comprobar estado',exact=True)).to_be_visible();page.unroute('**/economic-review/confirm')
        dialog.get_by_role('button',name='Comprobar estado',exact=True).click()
        expect(dialog.locator('.economic-saved')).to_have_text('Criterio guardado ✓');assert len(responses)==1 and responses[0]['events_created']==5
        after=page.request.get(BASE+'/__test/book').json()
        assert after['events']==9 and after['acceptances']==1 and after['originals_preserved']
        assert after['wallet_bookings']==2 and after['operator_bookings']==1 and after['order_proofs']==3
        assert D(after['positions']['WBNB'])==units and D(after['cost_basis']['WBNB'])==cost
        dialog.get_by_role('button',name='Cerrar revisión').click();assert page.evaluate('secondaryProviderListeners.size')==0
        page.locator('#real-lock').click();unlock(page);context()
        page.get_by_role('button',name='Revisar cuenta',exact=True).click();expect(dialog.locator('[data-er-source]')).to_have_count(2)
        with page.expect_response('**/economic-review/preview')as reopened:
            dialog.get_by_role('button',name='Revisar',exact=True).click()
        assert reopened.value.status==200,reopened.value.text()
        assert all(item['current']==item['network']for item in reopened.value.json()['review']['accounts'])
        final=page.request.get(BASE+'/__test/book').json();assert final['events']==9 and final['acceptances']==1
        assert final['cash']==after['cash'] and final['positions']==after['positions']
        storage=page.evaluate('JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])')
        assert fixture['wallet'].lower()not in storage.lower()and fixture['secondary_wallet'].lower()not in storage.lower()
        assert not page.evaluate('fixtureMethods.concat(fixtureSecondaryMethods).some(method=>/send|signTransaction|signTypedData/.test(method))')
        assert not errors,errors
        (out/'result.json').write_text(json.dumps({'passed':True,'real_socket_http':True,'discovered_providers':2,
            'signed_anvil_receipts':3,'wallet_contexts_separate':True,'exact_cost_and_units':True,'lost_reply_once':True,
            'encrypted_reopen':True,'wallet_sends':0,'owner_data_used':False,'errors':errors},indent=2))
        dialog.get_by_role('button',name='Cerrar revisión').click();page.locator('#real-lock').click()
    finally:
        (out/'final-dom.yml').write_text(page.locator('body').aria_snapshot());browser.close()
print('Two discovered wallets, genuine receipts, exact book, lost reply and reopen passed')
