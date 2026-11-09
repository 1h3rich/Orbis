"""DCA UI → BASE receipt → owner renewal → explicit rearm → DIP receipt."""
import json,os
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
average=os.environ.get('ORBIS_TEST_DEX_DCA_AVERAGE')=='1'
volume=os.environ.get('ORBIS_TEST_DEX_DCA_VOLUME')=='1'
paper_handoff=average or volume
base_spend='0,005' if average else '0,01'
total_spend='0,0075' if average else '0,02'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture,wallet_sends=True)
    page.goto(BASE+('/ui/#/dca' if paper_handoff else '/ui/#/real'));page.wait_for_load_state('networkidle')
    if paper_handoff:
        paper=page.locator('#dca-paper-session')
        paper.select_option(fixture['paper_session_id'])
        page.get_by_role('button',name='Preparar DEX',exact=True).click()
        expect(page).to_have_url(BASE+'/ui/#/real')
    parent=unlock(page)
    if paper_handoff:
        details=parent.locator(':scope > details')
        if not details.evaluate('element => element.open'):details.locator(':scope > summary').click()
        expect(parent.locator('[data-dex-bot-handoff]')).to_be_visible()
        parent.get_by_role('button',name='Verificar sesión seleccionada').click()
        expect(parent.locator('[name="source"]')).to_have_value('DCA:'+fixture['paper_session_id'])
        parent.locator('[name="research_validation_id"]').select_option(fixture['primary_reserve_id'])
        for name,value in {'entry_usdt':'0.01','budget_usdt':'0.06','exposure_usdt':'0.025','loss_usdt':'0.005',
                           'wbnb_quota':'1','monthly_usdt':'0.04'}.items():
            parent.locator(f'[name="{name}"]').fill(value)
        parent.locator('[data-dex-bot-review]').click()
        expect(parent.locator('[data-dex-bot-preview]')).to_contain_text(
            'Volumen MEXC ≥ 2× · 3 velas' if volume else 'Cierre bajo EMA 2 · ×0.00005 / ×0')
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_minutes"]').select_option('1440')
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[name="dex_run_binding"]')).to_have_value(fixture['binding_id'])
    expect(runs.locator('[name="dex_run_binding"] option:checked')).to_contain_text('DCA')
    expect(runs.locator('[name="dex_run_window"]')).to_be_visible()
    expect(runs.locator('[name="dex_run_window"]')).to_have_value('900')
    runs.locator('[name="dex_run_permission"]').check()
    with page.expect_response(lambda r:r.url.endswith('/dex-bots/runs') and r.request.method=='POST') as activated:
        runs.get_by_role('button',name='Activar',exact=True).click()
    response=activated.value;assert response.status==200,response.text()
    original=response.json()['id'];old=runs.locator('[data-dex-run-id="'+original+'"]')
    expect(old).to_contain_text('DCA · BNB/USDT · 1d')
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    expect(old).to_contain_text('Compra · Confirmada',timeout=20000)
    expect(old).to_contain_text('Compras · '+base_spend+' USDT · 1 activación')
    if average:expect(old).to_contain_text('Última señal · EMA 2 · ×0.00005')
    if volume:expect(old).to_contain_text('Volumen MEXC · 2×')
    old.get_by_role('button',name='Detener',exact=True).click();expect(old).to_contain_text('Detenido')
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    old.get_by_role('button',name='Renovar',exact=True).click()
    expect(owner.locator('[data-owner-review]')).to_contain_text('Renovar bot',timeout=15000)
    owner.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Operación confirmada',timeout=15000)
    owner.get_by_text('Operaciones del ejecutor',exact=True).click()
    owner.locator('[data-owner-history]').get_by_role('button',name='Aplicar renovación',exact=True).click()
    expect(owner.locator('[data-owner-status]')).to_contain_text('Renovación aplicada',timeout=15000)
    state=page.request.get(BASE+'/__test/state').json();assert state['sends']==1 and state['wallet_sends']==1
    expect(runs.locator('[name="dex_run_binding"]')).not_to_have_value(fixture['binding_id'])
    expect(runs.locator('[name="dex_run_window"]')).to_be_disabled()
    runs.locator('[name="dex_run_research"]').select_option(fixture['extra_reserve_id'])
    runs.locator('[name="dex_run_permission"]').check()
    with page.expect_response(lambda r:r.url.endswith('/rearm') and r.request.method=='POST') as rearmed:
        runs.get_by_role('button',name='Rearmar',exact=True).click()
    resumed=rearmed.value;assert resumed.status==200,resumed.text()
    current=resumed.json();assert current['source_kind']=='DCA' and current['previous_run_id']==original
    new=runs.locator('[data-dex-run-id="'+current['id']+'"]')
    expect(new).to_contain_text('Compras · '+base_spend+' USDT · 1 activación')
    expect(old).to_contain_text('Histórico · ')
    for _ in range(7):
        page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'tick'})
        page.wait_for_timeout(700)
        if page.request.get(BASE+'/__test/state').json()['sends']==2:break
    expect(new).to_contain_text('Compra · Confirmada',timeout=20000)
    expect(new).to_contain_text('Compras · '+total_spend+' USDT · 2 activaciones',timeout=20000)
    new.get_by_role('button',name='Detener',exact=True).click();expect(new).to_contain_text('Detenido')
    new.get_by_role('button',name='Gráfico',exact=True).click()
    graph=runs.locator('[data-dex-chart]')
    expect(graph.locator('[data-real-chart-canvas]')).to_have_attribute('data-fill-count','2')
    with page.expect_download() as downloaded:
        graph.get_by_role('button',name='Exportar JSONL',exact=True).click()
    rows=[json.loads(line) for line in downloaded.value.path().read_text().splitlines()]
    assert [row['signal']['activation_kind'] for row in rows]==['BASE','DIP']
    assert [row['provenance']['authorization_index'] for row in rows]==[1,2]
    assert [row['provenance']['reserve']['artifact_id'] for row in rows]==[fixture['primary_reserve_id'],fixture['extra_reserve_id']]
    assert all(row['provenance']['strategy_kind']=='DCA' for row in rows)
    if volume:
        assert all(row['signal']['volume']=={'volume_period':3,'volume_min_ratio':'2','volume_ratio':'2'} for row in rows)
    downloaded.value.save_as(output/'uniswap-dca-verified.jsonl')
    graph.get_by_role('button',name='Cerrar gráfico',exact=True).click()
    old.get_by_role('button',name='Gráfico',exact=True).click()
    expect(graph.locator('[data-real-chart-canvas]')).to_have_attribute('data-fill-count','1')
    with page.expect_download() as ancestor:
        graph.get_by_role('button',name='Exportar JSONL',exact=True).click()
    assert json.loads(ancestor.value.path().read_text())==rows[0]
    graph.get_by_role('button',name='Cerrar gráfico',exact=True).click()
    state=page.request.get(BASE+'/__test/state').json();assert state['sends']==2 and state['buys']==2 and state['sells']==0 and state['wallet_sends']==1
    runs.screenshot(path=str(output/'dca-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    runs.screenshot(path=str(output/'dca-mobile.png'))
    # Keep wallet RPC freshness checks on the same controlled time as chain.
    page.clock.set_system_time(datetime.fromtimestamp(state['clock_ms']/1000,timezone.utc))
    page.locator('#real-lock').click();parent=unlock(page)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Compras · '+total_spend+' USDT · 2 activaciones')
    expect(runs.locator('[data-run-history]')).not_to_contain_text('Activo')
    assert page.request.get(BASE+'/__test/state').json()['sends']==2
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and not errors,errors
    (output/'dca-result.json').write_text(json.dumps({'passed':True,'operator_sends':2,'wallet_sends':1,
        'base_and_dip_confirmed':True,'research_export_per_authorization':True,'remaining_limits_reviewed':True,'explicit_rearm':True,
        'historical_position_separated':True,'reopen_without_autorun':True,'errors':errors,
        **({'average_paper_handoff':True,'base_spend':base_spend,'total_spend':total_spend} if average else {}),
        **({'volume_paper_handoff':True,'volume_exact_jsonl':True} if volume else {})},indent=2))
    browser.close();print('DCA: BASE, STOP, owner renewal, explicit rearm, DIP, desktop/mobile and locked recovery')
