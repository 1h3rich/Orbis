"""Señal CLOSE → confirmación UI → trabajo revocable → recibo; GRVT ficticio."""
from tests.browser_network import route_once

import os
from decimal import Decimal

from playwright.sync_api import sync_playwright,expect

from tests.grvt_bracket_ui_fixture import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER


BASE='http://127.0.0.1:8799'
partial=os.environ.get('ORBIS_TEST_STRATEGY_CLOSE_PARTIAL')=='1'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100}); errors=[]; replies=[]; starts=[]
    page.on('request',lambda request:starts.append(request.url) if request.url.endswith('/strategy-close/start') else None)
    if os.environ.get('ORBIS_TEST_STRATEGY_CLOSE_DELAY_KEYS')=='1': page.clock.install()
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    source=page.request.get(BASE+'/__test/state').json()['source']; key='fake-testnet-key-'+source['sub_account_id']
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
        panel.locator('[name="api_key"]').fill(key)
        panel.locator('[name="dedicated_private_key"]').fill(KEY.hex())
        panel.locator('[name="expected_signer"]').fill(SIGNER)
    credentials(); panel.get_by_role('button',name='Firmar entrada · TESTNET',exact=True).click()
    group=panel.locator('[data-bracket-group]').first; expect(group).to_contain_text('Acuse recibido')
    consult=group.get_by_role('button',name='Consultar',exact=True)
    consult.focus(); page.wait_for_timeout(1800)
    expect(consult).to_be_focused()
    assert page.request.post(BASE+'/__test/close-source').ok
    panel.locator('[data-bracket-refresh]').click()
    if partial:
        panel.locator('[data-bracket-close-limits] > summary').click()
        panel.locator('[name="close_attempts"]').fill('1')
    expect(group.get_by_role('button',name='Cerrar por señal',exact=True)).to_be_visible()
    group.get_by_role('button',name='Cerrar por señal',exact=True).click()
    expect(panel.locator('[data-bracket-review]')).to_contain_text('Cierre de estrategia')
    if os.environ.get('ORBIS_TEST_STRATEGY_CLOSE_DELAY_KEYS')=='1': page.clock.fast_forward(21000)
    credentials()
    if os.environ.get('ORBIS_TEST_STRATEGY_CLOSE_STOP')!='1':
        def lose_reply(route):
            response=route.fetch(); replies.append({'status':response.status,'result':response.json()})
            assert response.status==202,response.text(); route.abort('failed')
        route_once(page,'**/strategy-close/start',lose_reply)
    panel.get_by_role('button',name='Ejecutar cierre · TESTNET',exact=True).click()
    expect(panel.locator('[name="dedicated_private_key"]')).to_have_value('')
    close=group.locator('[data-strategy-close]'); expect(close).to_be_visible()
    if os.environ.get('ORBIS_TEST_STRATEGY_CLOSE_STOP')=='1':
        close.get_by_role('button',name='Detener cierre',exact=True).click()
        expect(close).to_contain_text('Detenido',timeout=15000)
        assert 'Sin exposición conciliada' not in group.inner_text()
        assert page.request.post(BASE+'/__test/lock-sources').ok
        close.get_by_role('button',name='Continuar cierre',exact=True).click()
        credentials(); panel.get_by_role('button',name='Ejecutar cierre · TESTNET',exact=True).click()
    if partial:
        try: expect(close).to_contain_text('Límite de intentos',timeout=30000)
        except Exception:
            print({'panel':panel.locator('[data-bracket-state]').inner_text(),
                'venue':page.request.get(BASE+'/__test/state').json(),'replies':replies,'starts':starts,
                'form':panel.locator('[data-bracket-sign]').evaluate('(form)=>Array.from(form.elements).map(el=>({name:el.name,disabled:el.disabled,valid:el.validity.valid,missing:el.validity.valueMissing}))')},flush=True)
            raise
        expect(close.get_by_role('button',name='Continuar cierre',exact=True)).to_have_count(0)
        assert 'Sin exposición conciliada' not in group.inner_text()
    else:
        expect(close).to_contain_text('Completado',timeout=30000)
        expect(group).to_contain_text('Sin exposición conciliada')
    state=page.request.get(BASE+'/__test/state').json()
    assert Decimal(state['position'])==Decimal('0.4' if partial else '0')
    assert state['bulk_sends']==state['cancel_sends']==state['close_sends']==1
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (key,KEY.hex(),SIGNER): assert secret not in storage
    page.screenshot(path='/tmp/orbis-strategy-close-ui.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path='/tmp/orbis-strategy-close-mobile.png',full_page=True)
    page.locator('#real-lock').click(); page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-bracket'); panel.locator(':scope > summary').click()
    expect(panel.locator('[data-bracket-group]')).to_contain_text('BTC_USDT_Perp · 0.4' if partial else 'Sin exposición conciliada')
    assert not errors,errors
    browser.close()
    print('Cierre por señal UI: confirmación, ejecución sin clics intermedios, recibo, recuperación, móvil y secretos correctos · GRVT ficticio')
