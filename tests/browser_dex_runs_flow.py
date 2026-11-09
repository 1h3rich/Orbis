"""Real UI → protected API → operator signature → local EVM → confirmed UI."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.test_dex_operator_drafts import BACKUP_PASSWORD
from tests.browser_dex_runs_common import configure,unlock

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture)
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[name="dex_run_binding"]')).to_have_value(fixture['binding_id'])
    expect(runs.get_by_role('button',name='Activar',exact=True)).to_be_disabled()
    with page.expect_response('**/dex-bots/runs/activation-review') as reviewed:
        runs.get_by_role('button',name='Revisar saldo',exact=True).click()
    assert reviewed.value.ok and not reviewed.value.json()['automatic_execution'],reviewed.value.text()
    assert reviewed.value.request.headers['x-orbis-passive']=='1'
    funding=runs.locator('[data-run-funding-review]')
    expect(funding).to_contain_text('Gas cubierto')
    expect(funding).to_contain_text('Reserva STOP')
    expect(funding).to_contain_text('USDT utilizable')
    expect(funding).to_contain_text('WBNB utilizable')
    assert page.request.get(BASE+'/__test/state').json()['sends']==0
    runs.locator('[name="dex_run_permission"]').check()
    runs.get_by_text('Gas y ejecución',exact=True).click()
    runs.locator('[name="dex_run_price"]').fill('invalid')
    expect(funding).to_be_hidden()
    expect(runs.locator('[data-run-status]')).to_have_text('Revisa el saldo con los valores actuales.')
    runs.get_by_role('button',name='Activar',exact=True).click()
    expect(runs.locator('[data-run-status]')).to_contain_text('Importe decimal no válido')
    expect(runs.locator('[data-run-cancel]')).to_be_hidden()
    runs.locator('[name="dex_run_price"]').fill('1')
    runs.get_by_text('Gas y ejecución',exact=True).click()
    # Keep interception installed while aborting, so Chromium cannot strand
    # the immediately following recovery fetch. First stop a request before
    # dispatch, then lose the successful activation response, never repeat it.
    held=[];fault={'next':'hold'}
    def intercept(route):
        if route.request.method!='POST':return route.continue_()
        if fault['next']=='hold':held.append(route);return
        if fault['next']=='lose':
            fault['next']='pass';route.fetch();return route.abort('failed')
        return route.continue_()
    page.route('**/dex-bots/runs',intercept)
    runs.get_by_role('button',name='Activar',exact=True).click()
    expect(runs.get_by_role('button',name='Detener activación',exact=True)).to_be_enabled()
    runs.get_by_role('button',name='Detener activación',exact=True).click()
    expect(runs.locator('[data-run-status]')).to_contain_text('Parada local confirmada')
    assert len(held)==1
    held[0].continue_();fault['next']='lose'
    expect(runs.get_by_role('button',name='Activar',exact=True)).to_be_enabled()
    assert page.request.get(BASE+'/__test/state').json()['sends']==0
    runs.get_by_role('button',name='Activar',exact=True).click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Activo',timeout=15000)
    expect(runs.locator('[name="dex_run_permission"]')).not_to_be_checked()
    assert page.request.get(BASE+'/__test/state').json()['sends']==0
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    expect(runs.locator('[data-run-history]')).to_contain_text('Compra · Confirmada',timeout=20000)
    assert page.request.get(BASE+'/__test/state').json()['buys']==1
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'exit'})
    expect(runs.locator('[data-run-history]')).to_contain_text('Venta · Confirmada',timeout=20000)
    assert page.request.get(BASE+'/__test/state').json()['sells']==1
    runs.get_by_role('button',name='Detener',exact=True).click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Detenido')
    runs.screenshot(path=str(output/'runs-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    runs.screenshot(path=str(output/'runs-mobile.png'))
    page.locator('#real-lock').click();parent=unlock(page);runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Detenido')
    assert page.request.get(BASE+'/__test/state').json()['sends']==2
    assert page.evaluate("!(window.fixtureMethods||[]).includes('eth_sendTransaction')")
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and not errors,errors
    # A passive poll shortly before expiry must not renew the browser's view
    # permission; only actual user actions may keep the unlocked view alive.
    with page.expect_response(lambda r:'/dex-bots/runs' in r.url and r.request.method=='GET'):
        page.clock.fast_forward(890000)
    page.wait_for_timeout(200)
    page.clock.fast_forward(20000)
    expect(page.locator('#real-passphrase')).to_be_visible(timeout=5000)
    (output/'runs-result.json').write_text(json.dumps({'passed':True,'operator_sends':2,'wallet_sends':0,
        'buy_and_sell_confirmed':True,'stopped_reopen_without_autorun':True,'passive_poll_does_not_renew_view':True,
        'pre_dispatch_cancelled':True,'activation_response_lost_recovered_without_repeat':True,
        'passive_funding_review':True,'errors':errors},indent=2))
    browser.close();print('Protected DEX activation, one actual buy/sell, STOP and reopen without autorun')
