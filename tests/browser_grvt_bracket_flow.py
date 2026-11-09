"""Navegador → señal verificada → grupo nativo → recibo/cierre, sin fondos reales."""

import os
from decimal import Decimal

from playwright.sync_api import sync_playwright,expect

from tests.grvt_bracket_ui_fixture import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8799')
cancel=os.environ.get('ORBIS_TEST_BRACKET_CANCEL')=='1'

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100})
    errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    source=page.request.get(BASE+'/__test/state').json()['source']
    key='fake-testnet-key-'+source['sub_account_id']
    page.goto(BASE+'/ui/#/real'); page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-bracket'); expect(panel).to_be_visible()
    panel.locator(':scope > summary').click()
    expect(panel.locator('[name="source"] option').nth(1)).to_be_attached()
    panel.locator('[name="source"]').select_option(source['client_order_id'])
    for name,value in {'sub_account_id':source['sub_account_id'],'size':'0.5','limit_price':'100',
        'stop_loss':'95','take_profit':'110','entry_cap':'100','exit_cap':'100','api_key':key}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.get_by_role('button',name='Revisar entrada',exact=True).click()
    review=panel.locator('[data-bracket-review]'); expect(review).to_be_visible()
    expect(review).to_contain_text('STOP_LOSS')
    expect(panel.locator('[name="api_key"]')).to_have_value('')
    assert page.request.get(BASE+'/__test/state').json()['bulk_sends']==0
    panel.locator('[name="api_key"]').fill(key)
    panel.locator('[name="dedicated_private_key"]').fill(KEY.hex())
    panel.locator('[name="expected_signer"]').fill(SIGNER)
    panel.get_by_role('button',name='Firmar entrada · TESTNET',exact=True).click()
    group=panel.locator('[data-bracket-group]').first
    expect(group).to_be_visible()
    expect(group).to_contain_text('Envío incierto' if os.environ.get('ORBIS_TEST_BRACKET_LOST_REPLY')=='1' else 'Acuse recibido')
    assert page.request.get(BASE+'/__test/state').json()['bulk_sends']==1
    expect(panel.locator('[name="dedicated_private_key"]')).to_have_value('')
    panel.locator('[name="api_key"]').fill(key)
    group.get_by_role('button',name='Consultar',exact=True).click()
    expect(group).to_contain_text('FILLED')
    expect(group).to_contain_text('OPEN')
    if cancel:
        panel.locator('[name="api_key"]').fill(key)
        group.get_by_role('button',name='Cancelar salidas',exact=True).click()
        expect(group).to_contain_text('CANCELLED')
        expect(panel.locator('[data-bracket-page]')).to_contain_text('STOP activo')
    else:
        assert page.request.post(BASE+'/__test/trigger').ok
    for i in range(2):
        if i: page.wait_for_timeout(5200)
        panel.locator('[name="api_key"]').fill(key)
        group.get_by_role('button',name='Auditar',exact=True).click()
        expect(group.locator('summary')).to_contain_text(str(i+1)+' auditorías')
    expect(group.get_by_role('button',name='Conciliar',exact=True)).to_be_enabled()
    group.get_by_role('button',name='Conciliar',exact=True).click()
    expect(group).to_contain_text('Grupo conciliado')
    if cancel:
        panel.locator('[name="api_key"]').fill(key)
        group.get_by_role('button',name='Cerrar restante',exact=True).click()
        expect(review).to_be_visible(); expect(review).to_contain_text('Cierre de emergencia')
        panel.locator('[name="api_key"]').fill(key)
        panel.locator('[name="dedicated_private_key"]').fill(KEY.hex())
        panel.locator('[name="expected_signer"]').fill(SIGNER)
        panel.get_by_role('button',name='Firmar cierre · TESTNET',exact=True).click()
        emergency=group.locator('[data-emergency-id]').first
        expected_status='POST_CLAIMED' if os.environ.get('ORBIS_TEST_STRATEGY_CLOSE_LOST_REPLY')=='1' else 'ACKNOWLEDGED'
        expect(emergency).to_contain_text(expected_status)
        expect(group.get_by_role('button',name='Cerrar restante',exact=True)).to_have_count(0)
        for i in range(2):
            if i: page.wait_for_timeout(5200)
            panel.locator('[name="api_key"]').fill(key)
            emergency.get_by_role('button',name='Auditar cierre',exact=True).click()
            expect(emergency).to_contain_text(str(i+1)+' auditorías')
        expect(emergency.get_by_role('button',name='Conciliar cierre',exact=True)).to_be_enabled()
        emergency.get_by_role('button',name='Conciliar cierre',exact=True).click()
        expect(group).to_contain_text('Cierre conciliado')
    expect(group).to_contain_text('Sin exposición conciliada')
    state=page.request.get(BASE+'/__test/state').json()
    assert Decimal(state['position'])==0 and state['bulk_sends']==1
    assert state['cancel_sends']==(1 if cancel else 0) and state['close_sends']==(1 if cancel else 0)
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (key,KEY.hex(),SIGNER): assert secret not in storage
    page.screenshot(path='/tmp/orbis-grvt-bracket-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    page.screenshot(path='/tmp/orbis-grvt-bracket-mobile.png',full_page=True)
    assert page.request.post(BASE+'/__test/lock-sources').ok
    page.locator('#real-lock').click()
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-bracket'); panel.locator(':scope > summary').click()
    expect(panel.locator('[data-bracket-group]')).to_contain_text('Sin exposición conciliada')
    assert page.request.get(BASE+'/__test/state').json()['bulk_sends']==1
    page.locator('#real-hide').click()
    panel=page.locator('#real-grvt-bracket'); panel.locator(':scope > summary').click()
    expect(panel.get_by_role('button',name='Revisar entrada',exact=True)).to_be_disabled()
    expect(panel.locator('[data-bracket-groups]')).to_be_empty()
    assert not errors,errors
    browser.close()
    print('Entrada protegida UI: señal, revisión, tres órdenes, recuperación, recibo y '+
        ('emergencia' if cancel else 'objetivo')+' correctos; reapertura, móvil y secretos comprobados · GRVT ficticio')
