"""Recovered fill → owner renewal → researched permission → actual next fill."""
import json,os
from datetime import datetime,timezone
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
    fixture=page.request.get(BASE+'/__test/state').json()
    assert fixture['fixture_id']==output.name and fixture['recovered_fixture'] and fixture['initial_run_id']
    configure(page,fixture,wallet_sends=True)
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    if fixture['dca_fixture']:operator.locator('[name="operator_minutes"]').select_option('1440')
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    original=fixture['initial_run_id'];old=runs.locator('[data-dex-run-id="'+original+'"]')
    expect(old).to_contain_text('Compra · Confirmada')
    expect(old.get_by_role('button',name='Renovar',exact=True)).to_be_enabled()
    with page.expect_response(lambda r:r.url.endswith('/dex-bots/renewals') and r.request.method=='POST') as reviewed:
        old.get_by_role('button',name='Renovar',exact=True).click()
    response=reviewed.value;assert response.status==200,response.text();plan=response.json()
    expected='orbis.dex.bot.dca.policy-rollover.v2' if fixture['dca_fixture'] else 'orbis.dex.bot.policy-rollover.v2'
    assert plan['rollover']['schema']==expected and len(plan['rollover']['resolved_calls'])==1
    review=owner.locator('[data-owner-review]');expect(review).to_contain_text('Renovar bot')
    owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Operación confirmada',timeout=15000)
    owner.get_by_text('Operaciones del ejecutor',exact=True).click()
    card=owner.locator('[data-owner-operation="'+plan['id']+'"]')
    expect(card).to_contain_text('Renovación · Confirmado')
    card.get_by_role('button',name='Aplicar renovación',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Renovación aplicada',timeout=15000)
    expect(runs.locator('[name="dex_run_binding"]')).not_to_have_value(fixture['binding_id'])
    expect(runs.locator('[name="dex_run_research"] option')).to_have_count(3)
    runs.locator('[name="dex_run_research"]').select_option(fixture['primary_reserve_id'])
    runs.locator('[name="dex_run_permission"]').check()
    with page.expect_response(lambda r:r.url.endswith('/rearm') and r.request.method=='POST') as rearmed:
        runs.get_by_role('button',name='Rearmar',exact=True).click()
    response=rearmed.value;assert response.status==200,response.text();current=response.json()
    assert current['engine']==('orbis.dex.bot.dca.v3' if fixture['dca_fixture'] else 'orbis.dex.bot.long.v6')
    assert current['research_qualification']['evidence']['research']['artifact_id']==fixture['primary_reserve_id']
    assert current['previous_run_id']==original and current['position']['owned_wbnb']!='0'
    new=runs.locator('[data-dex-run-id="'+current['id']+'"]')
    state=page.request.get(BASE+'/__test/state').json();assert state['wallet_sends']==1 and state['sends']==0
    if fixture['dca_fixture']:
        expect(new).to_contain_text('Compras · 0,01 USDT · 1 activación')
        for _ in range(8):
            page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'tick'})
            page.wait_for_timeout(500)
            if page.request.get(BASE+'/__test/state').json()['sends']==1:break
        expect(new).to_contain_text('Compras · 0,02 USDT · 2 activaciones',timeout=20000)
    else:
        page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'exit'})
        expect(new).to_contain_text('Venta · Confirmada',timeout=20000)
        expect(new).to_contain_text('Posición · 0 WBNB')
    state=page.request.get(BASE+'/__test/state').json();assert state['sends']==1 and state['wallet_sends']==1
    new.get_by_role('button',name='Detener',exact=True).click();expect(new).to_contain_text('Detenido')
    runs.screenshot(path=str(output/'recovered-renewal-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    runs.screenshot(path=str(output/'recovered-renewal-mobile.png'))
    page.clock.set_system_time(datetime.fromtimestamp(state['clock_ms']/1000,timezone.utc))
    page.locator('#real-lock').click();parent=unlock(page)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Retomado')
    expect(runs.locator('[name="dex_run_research"]')).to_have_value(fixture['primary_reserve_id'])
    assert page.request.get(BASE+'/__test/state').json()['sends']==1
    assert not errors,errors
    (output/'recovered-renewal-result.json').write_text(json.dumps({'passed':True,'dca':fixture['dca_fixture'],
        'new_operator_sends':1,'owner_configure_sends':1,'recovered_fill_preserved':True,
        'explicit_researched_rearm':True,'reopen_without_autorun':True,'errors':errors},indent=2))
    browser.close()
