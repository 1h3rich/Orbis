"""Activación única → HOLD por stream → salida → recibo, con fronteras ficticias."""
from tests.browser_network import route_once

import os
import re
from playwright.sync_api import sync_playwright,expect

from tests.grvt_bracket_ui_fixture import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER

BASE='http://127.0.0.1:8799'
stop_first=os.environ.get('ORBIS_TEST_EXIT_WATCH_STOP')=='1'
stalled_recovery=os.environ.get('ORBIS_TEST_EXIT_WATCH_STALLED_RECOVERY')=='1'
native_exit=os.environ.get('ORBIS_TEST_NATIVE_EXIT','')
assert native_exit in ('','before','during')
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100}); errors=[]; starts=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('request',lambda request:starts.append(request.url) if request.url.endswith('/strategy-exit/start') else None)
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
    assert page.request.post(BASE+'/__test/lock-sources').ok
    if native_exit=='before': assert page.request.post(BASE+'/__test/trigger').ok
    held=[]
    if stalled_recovery:
        page.route('**/strategy-close',lambda route:held.append(route),times=1)
        page.route('**/testnet/brackets?*',lambda route:held.append(route))
    def activate(uncertain=False):
        group.get_by_role('button',name='Seguir salidas',exact=True).click()
        expect(panel.locator('[data-bracket-review]')).to_contain_text('Sólo esta posición')
        credentials(); panel.get_by_role('button',name='Activar salidas · TESTNET',exact=True).click()
        expect(panel.locator('[name="api_key"]')).to_have_value('')
        expect(panel.locator('[name="dedicated_private_key"]')).to_have_value('')
        expected=re.compile('Auditando grupo|Esperando auditoría|Completado') if native_exit=='before' else ('Activación sin confirmar' if uncertain else 'Observando')
        expect(group).to_contain_text(expected,timeout=15000)
    if not stop_first:
        def lose(route):
            response=route.fetch(); assert response.status==202,response.text(); route.abort('failed')
        route_once(page,'**/strategy-exit/start',lose)
    activate(stalled_recovery)
    if native_exit!='before':
        stop=group.get_by_role('button',name='Detener seguimiento',exact=True)
        stop.focus(); page.wait_for_timeout(1800); expect(stop).to_be_focused()
    state=page.request.get(BASE+'/__test/state').json()
    assert state['watch_connected']==(native_exit!='before') and state['cancel_sends']==state['close_sends']==0
    if stop_first or stalled_recovery:
        if stalled_recovery:
            with page.expect_response('**/strategy-close/stop') as stopped: stop.click()
            assert stopped.value.status==200
            # El acuse de STOP se muestra aunque el historial siga retenido.
            expect(group).to_contain_text(re.compile('Deteniendo|Detenido'))
        else: stop.click()
        assert page.request.get(BASE+'/__test/state').json()['close_sends']==0
        if stalled_recovery:
            page.unroute('**/strategy-close'); page.unroute('**/testnet/brackets?*')
            for route in held:
                try: route.abort('failed')
                except Exception: pass
            panel.locator('[data-bracket-refresh]').click()
        expect(group).to_contain_text('Detenido',timeout=10000)
        activate()
    if native_exit!='before': assert page.request.post(BASE+('/__test/trigger' if native_exit else '/__test/watch-trigger')).ok
    expect(group).to_contain_text('Completado',timeout=40000)
    expect(group).not_to_contain_text('Esperando precios')
    expect(group).to_contain_text('Sin exposición conciliada')
    state=page.request.get(BASE+'/__test/state').json()
    assert state['bulk_sends']==1 and state['cancel_sends']==state['close_sends']==(0 if native_exit else 1)
    assert state['watch_closed']==(native_exit!='before') and state['position']=='0.0'
    assert len(starts)==(2 if stop_first or stalled_recovery else 1)
    group.get_by_role('button',name='Historial de seguimiento',exact=True).click()
    history=group.locator('[data-watch-history]')
    expect(history).to_contain_text('Completado')
    history.get_by_role('button',name='Observaciones',exact=True).first.click()
    expect(history.locator('[data-watch-events]')).to_contain_text('Inicio')
    expect(history.locator('[data-watch-events]')).to_contain_text('Completado' if native_exit else 'Objetivo')
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (key,KEY.hex(),SIGNER): assert secret not in storage
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    group.scroll_into_view_if_needed(); page.screenshot(path='/tmp/orbis-exit-watch-mobile.png')
    page.locator('#real-lock').click()
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-bracket'); panel.locator(':scope > summary').click()
    expect(panel.locator('[data-bracket-group]')).to_contain_text('Completado')
    panel.get_by_role('button',name='Historial de seguimiento',exact=True).click()
    expect(panel.locator('[data-watch-history]')).to_contain_text('Completado')
    assert page.request.get(BASE+'/__test/state').json()['close_sends']==(0 if native_exit else 1)
    assert not errors,errors
    browser.close()
