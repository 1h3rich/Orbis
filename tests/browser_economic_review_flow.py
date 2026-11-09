"""GUI -> strict API -> signed mixed receipts -> atomic encrypted book -> reopen."""
import json,os
from decimal import Decimal
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock

BASE=os.environ['ORBIS_TEST_BASE_URL'];output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright()as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];responses=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    (output/'initial-dom.yml').write_text(page.locator('body').aria_snapshot())
    unlock(page)
    if page.locator('#real-account').input_value()!=fixture['account_id']:
        page.locator('#real-account').select_option(fixture['account_id'])
        page.get_by_role('button',name='Verificar control',exact=True).click()
        expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    before=page.request.get(BASE+'/__test/book').json();assert before['events']==4 and before['order_proofs']==0
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    dialog=page.get_by_role('dialog',name='Revisar cuenta',exact=True)
    expect(dialog.locator('[data-er-source]')).to_have_count(2)
    wallet=dialog.locator('[data-er-source="WALLET_SWAP:'+fixture['wallet_source_id']+'"]')
    wallet.locator('[data-er-trade]').select_option(fixture['manual_trade'])
    wallet.locator('[data-er-gas="'+fixture['manual_gas']+'"]').check()
    operator=dialog.locator('[data-er-source^="OPERATOR:"]')
    # The one old gas row is a candidate for both nearby receipts. Assign it
    # only to its real source and explicitly identify the other source's gas.
    operator.locator('[data-er-new-gas]').check()
    if operator.locator('[data-er-trade]').count():operator.locator('[data-er-trade]').select_option('new')
    dialog.screenshot(path=str(output/'selection-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    dialog.screenshot(path=str(output/'selection-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1000})
    with page.expect_response(lambda r:r.url.endswith('/economic-review/preview'))as preview:
        dialog.get_by_role('button',name='Revisar',exact=True).click()
    assert preview.value.status==200,preview.value.text()
    proposed=preview.value.json();assert proposed['review']['events_to_create']==3
    assert proposed['root_account_id']==fixture['ledger_id']!=fixture['account_id']
    expect(dialog.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_enabled()
    expect(dialog.locator('.economic-counts')).to_have_text('2 recibos · 3 asientos nuevos')
    assert fixture['wallet'].lower()not in preview.value.text().lower()
    now=page.request.get(BASE+'/__test/book').json();assert now['events']==4 and now['acceptances']==0 and now['order_proofs']==2
    assert now['contributions']==before['contributions']
    dialog.screenshot(path=str(output/'review-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    dialog.screenshot(path=str(output/'review-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1000})
    def lost(route):
        result=route.fetch();assert result.status==200,result.text();responses.append(result.json());route.abort('failed')
    page.route('**/economic-review/confirm',lost)
    dialog.get_by_role('button',name='Aceptar criterio',exact=True).click()
    expect(dialog.get_by_role('button',name='Comprobar estado',exact=True)).to_be_visible()
    page.unroute('**/economic-review/confirm')
    assert len(responses)==1 and responses[0]['events_created']==3
    dialog.get_by_role('button',name='Cerrar revisión').click()
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    dialog.get_by_role('button',name='Comprobar estado',exact=True).click()
    expect(dialog.locator('.economic-saved')).to_have_text('Criterio guardado ✓')
    after=page.request.get(BASE+'/__test/book').json()
    assert after['events']==7 and after['acceptances']==1 and after['operator_bookings']==after['wallet_bookings']==1
    assert after['originals_preserved']and after['contributions']=={'USDT':'100','BNB':'0.01'}
    assert Decimal(after['cash']['USDT'])<Decimal(before['cash']['USDT'])
    dialog.get_by_role('button',name='Cerrar revisión').click()
    page.locator('#real-lock').click();unlock(page)
    if page.locator('#real-account').input_value()!=fixture['account_id']:
        page.locator('#real-account').select_option(fixture['account_id'])
        page.get_by_role('button',name='Verificar control',exact=True).click()
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    expect(dialog.locator('[data-er-source]')).to_have_count(2)
    assert dialog.locator('[data-er-selected]').count()==0
    with page.expect_response(lambda r:r.url.endswith('/economic-review/preview'))as reviewed:
        dialog.get_by_role('button',name='Revisar',exact=True).click()
    assert reviewed.value.status==200,reviewed.value.text()
    current=reviewed.value.json()
    assert current['mode']=='EXISTING'and current['review']['events_to_create']==0
    assert all(item['current']==item['network']for item in current['review']['accounts'])
    unchanged=page.request.get(BASE+'/__test/book').json()
    assert unchanged['events']==7 and unchanged['acceptances']==1 and unchanged['originals_preserved']
    assert unchanged['cash']==after['cash']and unchanged['positions']==after['positions']
    assert page.evaluate('fixtureMethods.filter(m=>/send|signTransaction|signTypedData/.test(m))')==[]
    assert not errors,errors
    storage=page.evaluate('JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])')
    assert fixture['wallet'].lower()not in storage.lower()
    (output/'result.json').write_text(json.dumps({'passed':True,'mixed_receipts':2,'manual_trade_and_partial_gas_reused':True,
        'new_events':3,'lost_reply_recovered':True,'cipher_reopen':True,'actual_baseline':True,'owner_funds_used':False,
        'wallet_sends':0,'originals_preserved':True,'errors':errors,'book':unchanged},indent=2))
    dialog.get_by_role('button',name='Cerrar revisión').click();page.locator('#real-lock').click();browser.close()
print('Actual mixed signed receipts, manual matching, atomic acceptance, lost reply, mobile and encrypted reopen passed')
