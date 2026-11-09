"""Renovación explícita de cierre vencido, respuesta perdida y recuperación visual."""
from tests.browser_network import route_once
import os,json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.test_grvt_mainnet_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER
BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];renewals=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:renewals.append(request.url) if request.url.endswith('/renew') else None)
    state=page.request.get(BASE+'/__test/state').json();assert state['fixture_id']==output.name
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[name="sub_account_id"]')).to_have_value('123456')
    group=panel.locator('[data-mainnet-group]').first
    expect(group.get_by_role('button',name='Renovar cierre',exact=True)).to_be_disabled(timeout=20000)
    panel.locator('[data-mainnet-credentials] summary').click()
    for name,value in {'api_key':'synthetic-mainnet-api-key','dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():panel.locator('[name="'+name+'"]').fill(value)
    panel.locator('[data-mainnet-connect]').click();expect(panel.locator('[data-mainnet-connection]')).to_have_text('Firma conectada')
    group.get_by_role('button',name='Renovar cierre',exact=True).click()
    form=panel.locator('[data-mainnet-renew]');expect(form).to_be_visible()
    form.locator('[name="renew_ttl"]').fill('120');form.locator('[name="renew_attempts"]').fill('1')
    form.locator('[data-mainnet-renew-prepare]').click();expect(form.locator('[data-mainnet-renew-review]')).to_be_visible()
    expect(form.locator('[data-mainnet-renew-terms]')).to_contain_text('tope 100 USDT')
    expect(form.locator('[data-mainnet-renew-send]')).to_be_disabled()
    assert page.request.get(BASE+'/__test/state').json()['reductions']==0
    form.locator('[name="renew_ttl"]').fill('90');expect(form.locator('[data-mainnet-renew-review]')).to_be_hidden()
    form.locator('[name="renew_ttl"]').fill('120');form.locator('[data-mainnet-renew-prepare]').click()
    expect(form.locator('[data-mainnet-renew-review]')).to_be_visible();form.locator('[name="authorize_renewal"]').check()
    form.screenshot(path=str(output/'renewal-review-desktop.png'))
    def lost(route):
        response=route.fetch();assert response.status==200,response.text();route.abort('failed')
    route_once(page,'**/mainnet/groups/*/renew',lost);form.locator('[data-mainnet-renew-send]').click()
    expect(group).to_contain_text('Neto conciliado · 1.96 USDT',timeout=45000)
    expect(group).to_contain_text('Completado');assert len(renewals)==1
    group.screenshot(path=str(output/'renewal-complete-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    group.screenshot(path=str(output/'renewal-complete-mobile.png'))
    page.locator('#real-lock').click();page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[data-mainnet-group]').first).to_contain_text('Neto conciliado · 1.96 USDT',timeout=20000)
    expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key'):assert secret not in storage
    final=page.request.get(BASE+'/__test/state').json();assert final['reductions']==1 and not final['positions'] and not errors
    (output/'browser-result.json').write_text(json.dumps({'ok':True,'state':final,'page_errors':errors,'renewals':len(renewals)},indent=2))
    browser.close();print('MAINNET renewal browser passed',output)
