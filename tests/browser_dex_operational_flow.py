"""Bounded DEX operation behind a protected view, using an owned local EVM."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1050});errors=[];activations=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    def track(request):
        if request.method=='POST' and request.url.endswith('/dex-bots/runs'):activations.append(request)
    page.on('request',track)
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture)
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_minutes"]').select_option('60')
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[name="dex_run_binding"]')).to_have_value(fixture['binding_id'])
    runs.locator('[name="dex_run_minutes"]').fill('60')
    runs.locator('[name="dex_run_permission"]').check();runs.locator('[name="dex_run_background"]').check()
    with page.expect_response(lambda response:response.url.endswith('/dex-bots/runs') and response.request.method=='POST') as activated:
        runs.get_by_role('button',name='Activar',exact=True).click()
    response=activated.value;assert response.status==200,response.text()
    initial=response.json();assert initial['permission_mode']=='OPERATIONAL' and initial['execution_enabled']
    expect(runs.locator('[data-run-history]')).to_contain_text('Segundo plano')
    assert len(activations)==1
    token=activations[0].header_value('x-orbis-vault-token');assert token
    headers={'X-Orbis-Vault-Token':token}
    page.locator('#real-protect').click()
    expect(page.locator('[data-operational-status]')).to_contain_text('Operativa activa')
    assert page.request.get(BASE+'/real/accounts',headers=headers).status==401
    state=page.request.get(BASE+'/real/vault/status').json()
    assert state['unlocked'] and state['view_protected'] and state['operating']['until']==initial['expires_at']
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    page.wait_for_function("async () => (await (await fetch('/__test/state')).json()).buys===1",timeout=20000)
    parent=unlock(page);runs=parent.locator('[data-dex-runs]')
    with page.expect_response(lambda response:response.url.endswith('/dex-bots/runs') and response.request.method=='GET') as recovered:
        runs.locator(':scope > details > summary').click()
    restored=recovered.value.json();assert len(restored)==1
    assert restored[0]['expires_at']==initial['expires_at'] and restored[0]['execution_enabled']
    expect(runs.locator('[data-run-history]')).to_contain_text('Compra · Confirmada',timeout=20000)
    expect(runs.locator('[data-run-history]')).to_contain_text('Segundo plano')
    assert len(activations)==1
    runs.screenshot(path=str(output/'operational-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    runs.screenshot(path=str(output/'operational-mobile.png'))
    page.locator('#real-protect').click()
    expect(page.locator('[data-operational-status]')).to_contain_text('Operativa activa')
    with page.expect_response('**/real/operations/stop') as stopped:
        page.get_by_role('button',name='Detener y bloquear',exact=True).click()
    assert stopped.value.status==200 and stopped.value.json()['locked'],stopped.value.text()
    expect(page.locator('#real-passphrase')).to_be_visible()
    state=page.request.get(BASE+'/real/vault/status').json()
    assert not state['unlocked'] and not state.get('operating',{}).get('active',False)
    posts=page.request.get(BASE+'/__test/state').json()
    assert posts['sends']==1 and posts['buys']==1 and posts['sells']==0
    assert page.evaluate("!(window.fixtureMethods||[]).includes('eth_sendTransaction')")
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and not errors,errors
    (output/'operational-result.json').write_text(json.dumps({'passed':True,'operator_sends':1,'wallet_sends':0,
        'buy_while_view_protected':True,'reauthentication_does_not_renew':True,'activation_posts':len(activations),
        'hard_stop_revokes_without_liquidation':True,'errors':errors},indent=2))
    browser.close();print('DEX operational permission: protected-view buy, original expiry, hard STOP without liquidation')
