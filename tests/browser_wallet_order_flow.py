"""Original finalized manual wallet swap -> proof -> book; own signed Anvil tx."""
import json,os
from decimal import Decimal
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock

BASE=os.environ['ORBIS_TEST_BASE_URL'];output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json()
    assert fixture['fixture_id']==output.name and fixture['original_wallet']
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    (output/'initial-dom.yml').write_text(page.locator('body').aria_snapshot());unlock(page)
    def controls():
        host=page.locator('#real-wallet-swap');host.locator(':scope > details > summary').click()
        host.locator('summary',has_text='Historial DEX').click()
        return host,host.locator('[data-swap-action="'+fixture['source_id']+'"]')
    host,row=controls();expect(row).to_contain_text('Finalizado según RPC')
    before=page.request.get(BASE+'/__test/book').json();assert before['events']==2 and before['order_proofs']==0
    lost=[]
    def lose(route):
        result=route.fetch();assert result.status==200,result.text();lost.append(result.json());route.abort('failed')
    page.route('**/dex-receipts/WALLET_SWAP/*/order-proof',lose)
    row.get_by_role('button',name='Comprobar bloque',exact=True).click()
    expect(host.locator('[data-swap-state]')).to_contain_text('Bloque sin confirmar')
    expect(row.locator('[data-swap-order-saved]')).to_have_count(0)
    assert len(lost)==1 and lost[0]['created'] is True
    page.unroute('**/dex-receipts/WALLET_SWAP/*/order-proof')
    with page.expect_response(lambda r:r.url.endswith('/order-proof')) as response:
        row.get_by_role('button',name='Comprobar bloque',exact=True).click()
    assert response.value.status==200 and response.value.json()['created'] is False
    expect(row.locator('[data-swap-order-saved]')).to_have_text('Bloque ✓')
    proved=page.request.get(BASE+'/__test/book').json();assert proved=={**before,'order_proofs':1}
    row.get_by_role('button',name='Revisar registro',exact=True).click()
    panel=host.locator('[data-swap-booking]');expect(panel).to_be_visible()
    panel.get_by_role('button',name='Registrar',exact=True).click();expect(row).to_contain_text('contabilizado')
    after=page.request.get(BASE+'/__test/book').json();assert after['events']==4 and after['order_proofs']==1
    assert Decimal(after['cash']['USDT'])==Decimal('100')-Decimal(fixture['amount_units'])/10**18
    assert Decimal(after['positions']['WBNB'])==Decimal(fixture['amount_units'])/10**18
    page.locator('#real-lock').click();unlock(page);host,row=controls()
    with page.expect_response(lambda r:r.url.endswith('/order-proof')) as response:
        row.get_by_role('button',name='Comprobar bloque',exact=True).click()
    assert response.value.status==200 and response.value.json()['created'] is False
    expect(row.locator('[data-swap-order-saved]')).to_have_text('Bloque ✓')
    assert page.request.get(BASE+'/__test/book').json()==after
    host.screenshot(path=str(output/'wallet-order-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    host.screenshot(path=str(output/'wallet-order-mobile.png'))
    assert not page.evaluate("(window.fixtureMethods||[]).some(method=>/send|signTransaction/i.test(method))")
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})').lower()
    assert fixture['wallet'].lower() not in storage and fixture['tx_hash'].lower() not in storage
    assert not errors,errors
    (output/'result.json').write_text(json.dumps({'passed':True,'original_wallet':True,'lost_reply':True,
        'idempotent_reopen':True,'book':after,'wallet_sends':0,'errors':errors},indent=2)+'\n')
    page.locator('#real-lock').click();browser.close()
print('Own original wallet receipt, recovered proof, accounting, reopen and responsive layout passed')
