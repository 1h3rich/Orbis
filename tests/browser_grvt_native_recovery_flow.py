"""UI → permiso de recuperación → auditorías/recibo; proveedor controlado."""
from tests.browser_network import route_once

from decimal import Decimal
import os
from playwright.sync_api import sync_playwright,expect

from tests.grvt_bracket_ui_fixture import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
mode=os.environ['ORBIS_TEST_NATIVE_RECOVERY']
assert mode in ('ORPHAN','EXPIRED')

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100}); errors=[]; starts=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('request',lambda request:starts.append(request.url) if request.url.endswith('/native-recovery/start') else None)
    source=page.request.get(BASE+'/__test/state').json()['source']
    key='fake-testnet-key-'+source['sub_account_id']
    page.goto(BASE+'/ui/#/real'); page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-bracket'); panel.locator(':scope > summary').click()
    expect(panel.locator('[name="source"] option').nth(1)).to_be_attached()
    panel.locator('[name="source"]').select_option(source['client_order_id'])
    for name,value in {'sub_account_id':source['sub_account_id'],'size':'0.5','limit_price':'100',
        'stop_loss':'95','take_profit':'110','entry_cap':'100','exit_cap':'100','api_key':key}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.get_by_role('button',name='Revisar entrada',exact=True).click()
    expect(panel.locator('[data-bracket-review]')).to_be_visible()
    def credentials():
        for name,value in {'api_key':key,'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():
            panel.locator('[name="'+name+'"]').fill(value)
    credentials(); panel.get_by_role('button',name='Firmar entrada · TESTNET',exact=True).click()
    group=panel.locator('[data-bracket-group]').first
    expect(group).to_contain_text('Acuse recibido')
    assert page.request.post(BASE+'/__test/lock-sources').ok
    if mode=='EXPIRED': assert page.request.post(BASE+'/__test/expire-protections').ok
    panel.locator('[name="api_key"]').fill(key)
    group.get_by_role('button',name='Consultar',exact=True).click()
    expect(group.get_by_role('button',name='Recuperar grupo',exact=True)).to_be_visible()
    before=page.request.get(BASE+'/__test/state').json()
    assert before['bulk_sends']==1 and before['cancel_sends']==before['close_sends']==0
    group.get_by_role('button',name='Recuperar grupo',exact=True).click()
    expect(panel.locator('[data-bracket-review]')).to_contain_text('Recuperar grupo · TESTNET')
    expect(panel.locator('[data-bracket-review]')).to_contain_text('Sólo esta posición')
    credentials()
    def lose(route):
        response=route.fetch(); assert response.status==202,response.text(); route.abort('failed')
    route_once(page,'**/native-recovery/start',lose)
    panel.get_by_role('button',name='Recuperar · TESTNET',exact=True).click()
    expect(panel.locator('[name="api_key"]')).to_have_value('')
    expect(panel.locator('[name="dedicated_private_key"]')).to_have_value('')
    expect(group).to_contain_text('Completado',timeout=60000)
    expect(panel.locator('[data-bracket-state]')).not_to_contain_text('Activación no confirmada')
    expect(group).to_contain_text('Sin exposición conciliada')
    expect(group).to_contain_text('Sin ejecución' if mode=='ORPHAN' else 'Protección vencida')
    final=page.request.get(BASE+'/__test/state').json()
    assert final['bulk_sends']==final['cancel_sends']==1
    assert final['close_sends']==(0 if mode=='ORPHAN' else 1)
    assert Decimal(final['position'])==0 and not final['watch_connected']
    assert len(starts)==1
    group.get_by_role('button',name='Historial de seguimiento',exact=True).click()
    expect(group.locator('[data-watch-history]')).to_contain_text('Completado')
    group.scroll_into_view_if_needed(); page.screenshot(path='/tmp/orbis-native-recovery-'+mode.lower()+'-desktop.png')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    group.scroll_into_view_if_needed(); page.screenshot(path='/tmp/orbis-native-recovery-'+mode.lower()+'-mobile.png')
    page.locator('#real-lock').click()
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-bracket'); panel.locator(':scope > summary').click()
    expect(panel.locator('[data-bracket-group]').first).to_contain_text('Completado')
    assert page.request.get(BASE+'/__test/state').json()['close_sends']==final['close_sends']
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (key,KEY.hex(),SIGNER): assert secret not in storage
    assert not errors,errors
    browser.close()
