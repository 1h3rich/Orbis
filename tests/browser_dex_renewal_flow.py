"""UI policy renewal, one owner call and a causal resumed close in our EVM."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture,wallet_sends=True);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[name="dex_run_binding"]')).to_have_value(fixture['binding_id'])
    runs.locator('[name="dex_run_permission"]').check()
    with page.expect_response(lambda r:r.url.endswith('/dex-bots/runs') and r.request.method=='POST') as activated:
        runs.get_by_role('button',name='Activar',exact=True).click()
    response=activated.value;assert response.status==200,response.text()
    original=response.json()['id'];old=runs.locator('[data-dex-run-id="'+original+'"]')
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    expect(old).to_contain_text('Compra · Confirmada',timeout=20000)
    old.get_by_role('button',name='Detener',exact=True).click();expect(old).to_contain_text('Detenido')
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    # Losing a PREPARED response recovers the same review, without another
    # wallet request or a new budget.
    lost=[]
    def intercept(route):
        if lost:return route.continue_()
        result=route.fetch();assert result.status==200,result.text();lost.append(result.json());route.abort('failed')
    page.route('**/dex-bots/renewals',intercept)
    old.get_by_role('button',name='Renovar',exact=True).click()
    expect(old.get_by_role('button',name='Recuperar renovación',exact=True)).to_be_enabled()
    old.get_by_role('button',name='Recuperar renovación',exact=True).click()
    review=owner.locator('[data-owner-review]');expect(review).to_contain_text('Renovar bot',timeout=15000)
    assert len(lost)==1 and page.request.get(BASE+'/__test/state').json()['wallet_sends']==0
    owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Operación confirmada',timeout=15000)
    history=owner.locator('[data-owner-history]');owner.get_by_text('Operaciones del ejecutor',exact=True).click()
    card=history.locator('[data-owner-operation="'+lost[0]['id']+'"]')
    expect(card).to_contain_text('Renovación · Confirmado')
    card.get_by_role('button',name='Aplicar renovación',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Renovación aplicada',timeout=15000)
    state=page.request.get(BASE+'/__test/state').json();assert state['wallet_sends']==1 and state['sends']==1
    expect(runs.locator('[name="dex_run_binding"]')).not_to_have_value(fixture['binding_id'])
    runs.locator('[name="dex_run_permission"]').check()
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    with page.expect_response(lambda r:r.url.endswith('/rearm') and r.request.method=='POST') as rearmed:
        runs.get_by_role('button',name='Rearmar',exact=True).click()
    resumed=rearmed.value;assert resumed.status==200,resumed.text()
    current=resumed.json();assert current['engine']=='orbis.dex.bot.long.v4' and current['previous_run_id']==original
    assert current['root_binding_id']==fixture['binding_id']
    new=runs.locator('[data-dex-run-id="'+current['id']+'"]')
    expect(old).to_contain_text('Histórico · ')
    assert page.request.get(BASE+'/__test/state').json()['sends']==1
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'exit'})
    expect(new).to_contain_text('Venta · Confirmada',timeout=20000);expect(new).to_contain_text('Posición · 0 WBNB')
    new.get_by_role('button',name='Detener',exact=True).click();expect(new).to_contain_text('Detenido')
    runs.screenshot(path=str(output/'renewal-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    runs.screenshot(path=str(output/'renewal-mobile.png'))
    page.locator('#real-lock').click();parent=unlock(page)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Retomado')
    state=page.request.get(BASE+'/__test/state').json();assert state['sends']==2 and state['wallet_sends']==1
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and not errors,errors
    (output/'renewal-result.json').write_text(json.dumps({'passed':True,'operator_sends':2,'wallet_sends':1,
        'remaining_limits_reviewed':True,'lost_review_recovered':True,'explicit_rearm':True,
        'historical_position_separated':True,'reopen_without_autorun':True,'errors':errors},indent=2))
    browser.close();print('DEX renewal: buy, STOP, remaining review, owner configure, explicit rearm, sell and reopen')
