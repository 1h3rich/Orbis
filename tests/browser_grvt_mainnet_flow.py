"""Uso MAINNET en Chromium con HTTP de GRVT ficticio y bóveda real aislada."""
from tests.browser_network import route_once
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.test_grvt_mainnet_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER
BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];sends=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    responses=[]
    page.on('response',lambda response:responses.append({'url':response.url,'status':response.status}) if '/grvt/mainnet' in response.url else None)
    page.on('request',lambda request:sends.append(request.url) if request.url.endswith('/mainnet/send') else None)
    assert page.request.get(BASE+'/__test/state').json()['fixture_id']==output.name
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    panel.locator('[name="sub_account_id"]').fill('123456');panel.locator('[data-mainnet-refresh]').click()
    expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
    panel.screenshot(path=str(output/'mainnet-disconnected-desktop.png'))
    panel.locator('[data-mainnet-credentials] summary').click()
    for name,value in {'api_key':'synthetic-mainnet-api-key','dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    def connected(route):
        reply=route.fetch();assert reply.status==200,reply.text()
        for name in ('api_key','dedicated_private_key','expected_signer'):assert panel.locator('[name="'+name+'"]').input_value()==''
        route.fulfill(response=reply)
    # RAM conserva sesión/revisión aunque el lector de historial esté ocupado.
    def pending_history(route):
        reply=route.fetch();assert reply.status==200,reply.text()
        payload=reply.json();payload.update(history_pending=True,groups=[],stop=None,has_more=False)
        route.fulfill(response=reply,json=payload)
    page.route('**/mainnet?*',pending_history)
    page.route('**/mainnet/connect',connected,times=1)
    panel.locator('[data-mainnet-connect]').click()
    try:
        expect(panel.locator('[data-mainnet-connection]')).to_have_text('Firma conectada',timeout=30000)
    except Exception:
        print('Connection diagnostics:',panel.locator('[data-mainnet-state]').inner_text(),responses,flush=True)
        panel.screenshot(path=str(output/'connection-failure.png'))
        raise
    page.unroute('**/mainnet?*',pending_history)
    for name in ('api_key','dedicated_private_key','expected_signer'):expect(panel.locator('[name="'+name+'"]').first).to_have_value('')
    bot=panel.locator('[data-mainnet-bots]');bot.locator(':scope > summary').click()
    bot.locator('[name="bot_entry_cap"]').fill('75');bot.locator(':scope > summary').click()
    expect(panel.locator('[name="entry_cap"]')).to_have_value('150')
    panel.locator('[data-mainnet-size]').click()
    expect(panel.locator('[name="size"]')).to_have_value('1.499')
    for name,value in {'size':'0.5','entry_price':'100','stop_loss':'95','take_profit':'110','entry_cap':'100','exit_cap':'100'}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.locator('[data-mainnet-preview]').click();expect(panel.locator('[data-mainnet-review]')).to_be_visible()
    expect(panel.locator('[data-mainnet-send]')).to_be_disabled()
    assert page.request.get(BASE+'/__test/state').json()['sends']==0
    # Cambiar términos invalida la autorización antes del envío.
    panel.locator('[name="stop_loss"]').fill('94');expect(panel.locator('[data-mainnet-review]')).to_be_hidden()
    panel.locator('[name="stop_loss"]').fill('95');panel.locator('[data-mainnet-preview]').click()
    expect(panel.locator('[data-mainnet-review]')).to_be_visible()
    panel.locator('[name="authorize_entry"]').check()
    panel.screenshot(path=str(output/'mainnet-reviewed-desktop.png'))
    # El servidor recibe el POST; la respuesta se pierde en la frontera del navegador.
    def lose(route):
        reply=route.fetch();assert reply.status==200,reply.text();route.abort('failed')
    route_once(page,'**/mainnet/send',lose);panel.locator('[data-mainnet-send]').click()
    group=panel.locator('[data-mainnet-group]').first
    try:
        expect(group).to_contain_text('Acuse recibido',timeout=30000)
    except Exception:
        print('Entry diagnostics:',panel.locator('[data-mainnet-state]').inner_text(),page.request.get(BASE+'/__test/state').json(),responses,flush=True)
        panel.screenshot(path=str(output/'entry-failure.png'))
        raise
    expect(panel.locator('[data-mainnet-review]')).to_be_hidden()
    assert page.request.get(BASE+'/__test/state').json()['sends']==1 and len(sends)==1
    page.locator('#real-lock').click()
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel.locator(':scope > summary').click()
    panel.locator('[name="sub_account_id"]').fill('123456')
    panel.locator('[data-mainnet-refresh]').click()
    expect(group).to_be_visible(timeout=30000)
    expect(group.locator('[data-mainnet-consult]')).to_be_disabled()

    panel.locator('[data-mainnet-view-credentials] summary').click()
    view_key=panel.locator('[name="view_api_key"]')
    view_key.fill('synthetic-mainnet-view-key')
    with page.expect_request('**/mainnet/connect-view') as view_request:
        panel.locator('[data-mainnet-connect-view]').click()
    assert set(view_request.value.post_data_json)=={'sub_account_id','permission_revision','api_key'}
    expect(view_key).to_have_value('')
    expect(panel.locator('[data-mainnet-connection]')).to_have_text('Sólo lectura')
    expect(group.locator('[data-mainnet-close-position]')).to_be_disabled()
    expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
    assert page.request.post(BASE+'/__test/partial').ok
    group.locator('[data-mainnet-consult]').click()
    expect(group).to_contain_text('TP · CANCELLED · 0.2')
    assert page.request.get(BASE+'/__test/state').json()['reductions']==0

    panel.locator('[data-mainnet-credentials] summary').click()
    for name,value in {'api_key':'synthetic-mainnet-api-key','dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.locator('[data-mainnet-connect]').click()
    expect(group.locator('[data-mainnet-close-position]')).to_be_enabled()
    group.locator('[data-mainnet-close-position]').click()
    expect(panel.locator('[data-mainnet-close]')).to_be_visible()
    expect(panel.locator('[data-mainnet-confirm-close]')).to_be_disabled()
    panel.locator('[name="authorize_close"]').check()
    page.route('**/mainnet?*',pending_history)
    route_once(page,'**/mainnet/groups/*/recover',lose)
    panel.locator('[data-mainnet-confirm-close]').click()
    try:
        expect(group).to_contain_text('Neto conciliado · 1.96 USDT',timeout=45000)
    except Exception:
        print('Close diagnostics:',panel.locator('[data-mainnet-state]').inner_text(),page.request.get(BASE+'/__test/state').json(),responses,flush=True)
        panel.screenshot(path=str(output/'close-failure.png'))
        raise
    page.unroute('**/mainnet?*',pending_history)
    panel.locator('[data-mainnet-refresh]').click()
    assert page.request.get(BASE+'/__test/state').json()['reductions']==1
    expect(group).to_contain_text('Cierre IOC · FILLED · 0.3')
    expect(group).to_contain_text('Completado')
    group.locator('summary').click()
    panel.locator('[data-mainnet-refresh]').click()
    expect(panel.locator('[data-mainnet-refresh]')).to_be_enabled()
    expect(group.locator('details')).to_have_attribute('open','')
    group.scroll_into_view_if_needed();panel.screenshot(path=str(output/'mainnet-closed-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});panel.scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    panel.screenshot(path=str(output/'mainnet-closed-mobile.png'))
    # El parcial activó STOP de revisión; sólo el recibo permite rearmar.
    expect(panel.locator('[data-mainnet-connection]')).to_have_text('STOP activo')
    page.on('dialog',lambda dialog:dialog.accept())
    panel.locator('[data-mainnet-rearm]').click()
    expect(panel.locator('[data-mainnet-preview]')).to_be_enabled()
    for name,value in {'size':'0.5','entry_price':'100','stop_loss':'95','take_profit':'110','entry_cap':'100','exit_cap':'100'}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    # STOP gana a una revisión cuya respuesta ya llega del servidor.
    def pending_after_stop(route):
        reply=route.fetch();assert reply.ok,reply.text()
        payload=reply.json();payload.update(history_pending=True,groups=[],stop=None,has_more=False)
        route.fulfill(response=reply,json=payload)
    def stop_review(route):
        reply=route.fetch();assert reply.status==200,reply.text()
        page.route('**/mainnet?*',pending_after_stop)
        with page.expect_response(lambda response:response.url.endswith('/mainnet/stop'),timeout=30000) as stopped:
            panel.locator('[data-mainnet-stop]').click()
        assert stopped.value.ok and stopped.value.json()['stopped'] is True
        expect(panel.locator('[data-mainnet-connection]')).to_have_text('STOP activo')
        expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
        route.fulfill(response=reply)
    page.route('**/mainnet/preview',stop_review,times=1)
    panel.locator('[data-mainnet-preview]').click()
    expect(panel.locator('[data-mainnet-review]')).to_be_hidden()
    expect(panel.locator('[data-mainnet-connection]')).to_have_text('STOP activo')
    expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
    page.unroute('**/mainnet?*',pending_after_stop)
    # Una consulta local ocupada no se interpreta como historial vacío.
    def pending_subaccounts(route):
        route.fulfill(status=200,content_type='application/json',body='{"network":"MAINNET","pending":true,"sub_account_ids":[]}')
    page.route('**/mainnet/subaccounts',pending_subaccounts,times=1)
    page.locator('#real-lock').click();page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[name="sub_account_id"]')).to_have_value('123456')
    panel.locator('[data-mainnet-refresh]').click()
    expect(panel.locator('[data-mainnet-group]').first).to_contain_text('Neto conciliado · 1.96 USDT')
    expect(panel.locator('[data-mainnet-connection]')).to_have_text('STOP activo')
    expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
    page.locator('#real-hide').click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[data-mainnet-connection]')).to_have_text('Importes ocultos')
    expect(panel.locator('[data-mainnet-group]')).to_have_count(0)
    expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
    page.locator('#real-hide').click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[name="sub_account_id"]')).to_have_value('123456')
    expect(panel.locator('[data-mainnet-group]').first).to_contain_text('Neto conciliado · 1.96 USDT')
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key','synthetic-mainnet-view-key'):assert secret not in storage
    assert not errors,errors
    final=page.request.get(BASE+'/__test/state').json();assert final['sends']==1 and final['reductions']==1
    (output/'browser-result.json').write_text(json.dumps({'ok':True,'state':final,'page_errors':errors,'entry_requests':len(sends),'view_after_unlock':True,'view_reduction_orders':0},indent=2))
    browser.close();print('MAINNET browser flow passed',output)
