"""Browser → REAL API → original replays → scoped private reads → comparison."""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

BASE=os.environ['ORBIS_TEST_BASE_URL']
OUT=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    stale_status=[]
    page.route('**/real/exchanges/grvt/status',lambda route:stale_status.append(route))
    page.goto(BASE+'/ui/#/real')
    page.locator('#real-passphrase').fill('owned browser REAL review password')
    page.locator('#real-passphrase-confirm').fill('owned browser REAL review password')
    page.get_by_role('button',name='Crear bóveda',exact=True).click()
    page.locator('#real-new-account [name="name"]').fill('Owned review account')
    page.get_by_role('button',name='Crear cuenta',exact=True).click()
    page.locator('#real-grvt-connect summary').click()
    page.locator('#real-grvt-form [name="api_key"]').fill('owned-browser-view-key')
    with page.expect_response('**/real/exchanges/grvt/connect') as connected:
        page.get_by_role('button',name='Consultar GRVT',exact=True).click()
    assert connected.value.status==200,connected.value.text()
    root=page.locator('#real-perp-live-review')
    expect(root).to_be_visible()
    assert len(stale_status)==1
    stale_status[0].fulfill(status=200,json={'connected':False})
    page.wait_for_load_state('networkidle')
    expect(root).to_be_visible()
    page.unroute('**/real/exchanges/grvt/status')
    root.locator('summary').first.click()
    fixture=page.request.get(BASE+'/__test/state').json()
    select=root.locator('[data-perp-live-source]')
    expect(select.locator('option')).to_have_count(2)
    select.select_option(fixture['session_id'])
    root.locator('[data-perp-live-sub]').select_option('123')
    with page.expect_response('**/real/exchanges/grvt/perp-review') as reviewed:
        root.get_by_role('button',name='Revisar',exact=True).click()
    assert reviewed.value.status==200,reviewed.value.text()
    result=reviewed.value.json()
    assert result['blockers']==['PERP_EXECUTOR_NOT_IMPLEMENTED']
    expect(root.locator('[data-perp-live-result]')).to_contain_text('1 operación PAPER cerrada')
    expect(root.locator('[data-perp-live-result]')).to_contain_text('Para operar, revisa y autoriza en Bot · MAINNET')
    assert root.get_by_role('button',name='Activar',exact=True).count()==0
    root.screenshot(path=str(OUT/'review-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    root.screenshot(path=str(OUT/'review-mobile.png'))
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+2')
    held=[]
    page.route('**/real/exchanges/grvt/perp-review',lambda route:held.append(route))
    root.get_by_role('button',name='Revisar',exact=True).click()
    expect(root.locator('[data-perp-live-state]')).to_have_text('Reproduciendo y comparando…')
    page.get_by_role('button',name='Desconectar',exact=True).click()
    expect(root).to_be_hidden()
    assert len(held)==1
    held[0].fulfill(status=200,json=result)
    page.unroute('**/real/exchanges/grvt/perp-review')
    expect(root.locator('[data-perp-live-result]')).to_be_empty()
    page.locator('#real-grvt-connect summary').click()
    page.locator('#real-grvt-form [name="api_key"]').fill('owned-browser-view-key')
    page.get_by_role('button',name='Consultar GRVT',exact=True).click()
    expect(root).to_be_visible()
    root.locator('summary').first.click()
    expect(select.locator('option')).to_have_count(2)
    select.select_option(fixture['session_id'])
    root.locator('[data-perp-live-sub]').select_option('123')
    page.request.post(BASE+'/__test/mismatch')
    with page.expect_response('**/real/exchanges/grvt/perp-review') as mismatch:
        root.get_by_role('button',name='Revisar',exact=True).click()
    assert mismatch.value.status==200,mismatch.value.text()
    expect(root.locator('[data-perp-live-result]')).to_contain_text('El modo de margen difiere')
    expect(root.locator('[data-perp-live-result]')).to_contain_text('El apalancamiento difiere')
    expect(root.locator('[data-perp-live-result]')).to_contain_text('El perfil supera los límites de esta cuenta')
    page.request.post(BASE+'/__test/lock-paper')
    with page.expect_response('**/real/exchanges/grvt/perp-review') as locked:
        root.get_by_role('button',name='Revisar',exact=True).click()
    assert locked.value.status==423
    expect(root.locator('[data-perp-live-result]')).to_be_empty()
    assert 'owned-browser-view-key' not in page.locator('body').inner_text()
    calls=page.request.get(BASE+'/__test/state').json()['private_calls']
    assert sum(url.endswith('/get_all_initial_leverage') for url in calls)==4
    assert all(url.endswith(('/login','/aggregated_account_summary','/account_summary','/get_all_initial_leverage')) for url in calls)
    page.get_by_role('button',name='Desconectar',exact=True).click()
    expect(root).to_be_hidden()
    assert not errors,errors
    (OUT/'review-proof.json').write_text(json.dumps({'review':result,'private_calls':calls,'page_errors':errors,
        'wallet_actions':False,'real_orders':False,'original_replayed':True},indent=2))
    browser.close()
