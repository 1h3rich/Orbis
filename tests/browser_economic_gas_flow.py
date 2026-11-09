"""Actual gas opt-in -> signed proofs -> atomic review -> reopen in Chromium."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure
from tests.test_wallet_swap import PASSWORD

BASE=os.environ['ORBIS_TEST_BASE_URL'];output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];confirmed=[];proofs=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('response',lambda response:proofs.append(response.url)if response.url.endswith(('/order-proof','/gas-order-proof')) and response.status==200 else None)
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    def unlock():
        page.locator('#real-passphrase').fill(PASSWORD)
        page.get_by_role('button',name='Desbloquear',exact=True).click()
        page.locator('#real-account').select_option(fixture['account_id'])
        page.get_by_role('button',name='Conectar wallet',exact=True).click()
        expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
        page.get_by_role('button',name='Verificar control',exact=True).click()
        expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    unlock();before=page.request.get(BASE+'/__test/book').json()
    assert before['gas_proofs']==before['trade_proofs']==before['acceptances']==0
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    dialog=page.get_by_role('dialog',name='Revisar cuenta',exact=True)
    expect(dialog.locator('[data-er-source]')).to_have_count(0 if fixture['booked'] else 1)
    expect(dialog.locator('[data-er-include-gas]')).not_to_be_checked()
    dialog.locator('[data-er-include-gas]').check()
    expect(dialog.locator('[data-er-source]')).to_have_count(1 if fixture['booked'] else 2)
    gas=dialog.locator('[data-er-source="CALL_RESOLUTION:'+fixture['gas_id']+'"]')
    expect(gas.locator('strong')).to_have_text('Gas · Permiso')
    expect(gas).not_to_contain_text('WBNB')
    expect(gas.locator('[data-er-trade]')).to_have_count(0)
    if not fixture['booked']:
        financial=dialog.locator('[data-er-source="WALLET_SWAP:'+fixture['trade_id']+'"]')
        financial.locator('[data-er-trade]').select_option(fixture['manual_trade'])
        gas.locator('[data-er-gas="'+fixture['manual_gas']+'"]').check()
        financial.locator('[data-er-new-gas]').check()
    dialog.screenshot(path=str(output/'selection-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    header=dialog.locator('.economic-review-header').bounding_box()
    footer=dialog.locator('footer').bounding_box();body_box=dialog.locator('[data-er-body]').bounding_box()
    assert header and header['height']>50 and header['y']>=0 and header['y']+header['height']<=844
    assert footer and footer['height']>40 and footer['y']+footer['height']<=844
    assert body_box and body_box['height']>100
    dialog.screenshot(path=str(output/'selection-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1000})
    with page.expect_response(lambda r:r.url.endswith('/economic-review/preview')) as proposed:
        dialog.get_by_role('button',name='Revisar',exact=True).click()
    assert proposed.value.status==200,proposed.value.text()
    preview=proposed.value.json();new_events=0 if fixture['booked'] else 2
    assert preview['mode']==('EXISTING'if fixture['booked'] else 'BATCH')
    assert preview['review']['checked_receipts']==(1 if fixture['booked'] else 2)
    assert preview['review']['events_to_create']==new_events
    assert fixture['wallet'].lower()not in proposed.value.text().lower()
    now=page.request.get(BASE+'/__test/book').json()
    assert now['events']==before['events'] and now['acceptances']==0
    assert now['gas_proofs']==1 and now['trade_proofs']==(0 if fixture['booked'] else 1)
    dialog.screenshot(path=str(output/'review-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    header=dialog.locator('.economic-review-header').bounding_box()
    footer=dialog.locator('footer').bounding_box();body_box=dialog.locator('[data-er-body]').bounding_box()
    assert header and header['height']>50 and header['y']>=0 and header['y']+header['height']<=844
    assert footer and footer['height']>40 and footer['y']+footer['height']<=844
    assert body_box and body_box['height']>250
    dialog.screenshot(path=str(output/'review-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1000})
    def lost(route):
        response=route.fetch();assert response.status==200,response.text();confirmed.append(response.json());route.abort('failed')
    page.route('**/economic-review/confirm',lost)
    dialog.get_by_role('button',name='Aceptar criterio',exact=True).click()
    expect(dialog.get_by_role('button',name='Comprobar estado',exact=True)).to_be_visible()
    page.unroute('**/economic-review/confirm')
    assert len(confirmed)==1
    dialog.get_by_role('button',name='Cerrar revisión').click()
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    dialog.get_by_role('button',name='Comprobar estado',exact=True).click()
    expect(dialog.locator('.economic-saved')).to_have_text('Criterio guardado ✓')
    after=page.request.get(BASE+'/__test/book').json()
    assert after['events']==before['events']+new_events and after['acceptances']==1
    assert after['originals_preserved'] and after['contributions']==before['contributions']
    assert after['positions']==before['positions'] and after['cash']['USDT']==before['cash']['USDT']
    assert after['gas_bookings']==1 and after['wallet_bookings']==(0 if fixture['booked'] else 1)
    dialog.get_by_role('button',name='Cerrar revisión').click();page.locator('#real-lock').click();unlock()
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    dialog.locator('[data-er-include-gas]').check()
    expect(dialog.locator('[data-er-source]')).to_have_count(1 if fixture['booked'] else 2)
    assert dialog.locator('[data-er-selected]').count()==0
    with page.expect_response(lambda r:r.url.endswith('/economic-review/preview')) as reread:
        dialog.get_by_role('button',name='Revisar',exact=True).click()
    assert reread.value.status==200,reread.value.text()
    data=reread.value.json();assert data['mode']=='EXISTING' and data['review']['events_to_create']==0
    assert all(a['current']==a['network']for a in data['review']['accounts'])
    final=page.request.get(BASE+'/__test/book').json()
    assert final['events']==after['events'] and final['acceptances']==1 and final['originals_preserved']
    assert final['cash']==after['cash'] and final['positions']==after['positions']
    assert len(proofs)==(1 if fixture['booked'] else 2)
    assert page.evaluate('fixtureMethods.filter(m=>/send|signTransaction|signTypedData/.test(m))')==[]
    assert not errors,errors
    storage=page.evaluate('JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])')
    assert fixture['wallet'].lower()not in storage.lower()
    (output/'result.json').write_text(json.dumps({'passed':True,'mode':'BOOKED_OPT_IN'if fixture['booked'] else 'NEW_MIXED',
        'actual_signed_receipts':True,'gas_only_has_no_trade':True,'new_events':new_events,'lost_reply_recovered':True,
        'encrypted_reopen':True,'manual_partial_gas_and_trade_reused':not fixture['booked'],'wallet_sends':0,
        'owner_funds_used':False,'book':final,'errors':errors},indent=2))
    dialog.get_by_role('button',name='Cerrar revisión').click();page.locator('#real-lock').click();browser.close()
print('Actual gas catalogue, proof, review, lost reply, mobile and encrypted reopen passed')
