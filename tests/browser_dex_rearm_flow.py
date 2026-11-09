"""UI rearm after a real fixture buy: preserve ownership and never repeat POST."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];requests=[];lost=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('request',lambda request:requests.append(request) if request.method=='POST' and request.url.endswith('/rearm') else None)
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[name="dex_run_binding"]')).to_have_value(fixture['binding_id'])
    expect(runs.locator('[name="dex_run_research"] option')).to_have_count(3)
    runs.locator('[name="dex_run_research"]').select_option(fixture['extra_reserve_id'])
    runs.locator('[name="dex_run_permission"]').check()
    with page.expect_response(lambda response:response.url.endswith('/dex-bots/runs') and response.request.method=='POST') as activated:
        runs.get_by_role('button',name='Activar',exact=True).click()
    response=activated.value;assert response.status==200,response.text()
    original=response.json()['id'];token=response.request.header_value('x-orbis-vault-token')
    assert response.json()['research_qualification']['evidence']['research']['artifact_id']==fixture['extra_reserve_id']
    headers={'X-Orbis-Vault-Token':token};base=f"/real/accounts/{fixture['account_id']}/dex-bots/runs"
    def detail(identifier):
        response=page.request.get(BASE+base+'/'+identifier,headers=headers);assert response.status==200,response.text()
        return response.json()
    old=runs.locator('[data-dex-run-id="'+original+'"]')
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    expect(old).to_contain_text('Compra · Confirmada',timeout=20000)
    old.get_by_role('button',name='Detener',exact=True).click();expect(old).to_contain_text('Detenido')
    before=detail(original);assert int(before['position']['owned_wbnb'])>0
    runs.locator('[name="dex_run_research"]').select_option('')
    runs.get_by_role('button',name='Actualizar bots',exact=True).click()
    expect(runs.locator('[name="dex_run_research"]')).to_have_value(fixture['extra_reserve_id'])
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    def intercept(route):
        if lost:return route.continue_()
        response=route.fetch();assert response.status==200,response.text()
        lost.append(response.json());route.abort('failed')
    page.route('**/rearm',intercept)
    runs.locator('[name="dex_run_permission"]').check()
    runs.get_by_role('button',name='Rearmar',exact=True).click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Retomado',timeout=15000)
    assert len(lost)==1 and len(requests)==1
    current=lost[0];assert current['position']==before['position'] and current['previous_run_id']==original
    assert current['research_qualification']['evidence']['research']['artifact_id']==fixture['extra_reserve_id']
    assert page.request.get(BASE+'/__test/state').json()['sends']==1
    expect(runs.locator('[name="dex_run_permission"]')).not_to_be_checked()
    new=runs.locator('[data-dex-run-id="'+current['id']+'"]')
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'exit'})
    expect(new).to_contain_text('Venta · Confirmada',timeout=20000)
    expect(old).to_contain_text('Histórico · ');expect(new).to_contain_text('Posición · 0 WBNB')
    assert detail(current['id'])['position']['owned_wbnb']=='0'
    assert len(detail(original)['intents'])==1 and page.request.get(BASE+'/__test/state').json()['sends']==2
    new.get_by_role('button',name='Detener',exact=True).click();expect(new).to_contain_text('Detenido')
    runs.screenshot(path=str(output/'rearm-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    runs.screenshot(path=str(output/'rearm-mobile.png'))
    page.locator('#real-lock').click();parent=unlock(page)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Retomado')
    expect(runs.locator('[name="dex_run_research"]')).to_have_value(fixture['extra_reserve_id'])
    assert page.request.get(BASE+'/__test/state').json()['sends']==2
    assert page.evaluate("!(window.fixtureMethods||[]).includes('eth_sendTransaction')")
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and not errors,errors
    (output/'rearm-result.json').write_text(json.dumps({'passed':True,'operator_sends':2,'wallet_sends':0,
        'position_preserved':True,'rearm_response_lost_recovered_without_repeat':True,
        'rearm_posts':len(requests),'old_epoch_receipts_isolated':True,'reopen_without_autorun':True,'errors':errors},indent=2))
    browser.close();print('DEX rearm: buy, STOP, one resumed permission, sell, response loss and reopen without autorun')
