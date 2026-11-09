"""UI buy → sell → STOP → exact owner BNB return; only our disposable EVM."""
import json,os,time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name and fixture['return_fixture']
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[name="dex_run_binding"]')).to_have_value(fixture['binding_id'])
    runs.locator('[name="dex_run_permission"]').check();runs.get_by_role('button',name='Activar',exact=True).click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Activo',timeout=15000)
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    expect(runs.locator('[data-run-history]')).to_contain_text('Compra · Confirmada',timeout=20000)
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'exit'})
    expect(runs.locator('[data-run-history]')).to_contain_text('Venta · Confirmada',timeout=20000)
    runs.get_by_role('button',name='Detener',exact=True).click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Detenido')
    deadline=time.monotonic()+10
    while not page.request.get(BASE+'/__test/workers-idle').json()['idle']:
        assert time.monotonic()<deadline;page.wait_for_timeout(100)
    recovery=parent.locator('[data-dex-return]');recovery.locator(':scope > details > summary').click()
    expect(recovery.locator('[name="dex_return_binding"]')).to_have_value(fixture['binding_id'])
    before=page.request.get(BASE+'/__test/state').json();assert before['native_returns']==0 and int(before['operator_balance_wei'])>21000*10**9
    with page.expect_response(lambda r:r.url.endswith('/dex-gas-returns') and r.request.method=='POST') as reviewed:
        recovery.get_by_role('button',name='Revisar devolución',exact=True).click()
    response=reviewed.value;assert response.status==200,response.text()
    record=response.json();assert int(record['amount_wei'])==int(before['operator_balance_wei'])-21000*10**9
    expect(recovery.locator('[data-return-preview]')).to_contain_text('A tu wallet conectada')
    # The native send succeeds while its HTTP response is lost. Recovery uses
    # GET and the existing record, never a second signature or transfer POST.
    lost=[]
    def intercept(route):
        result=route.fetch();assert result.status==200,result.text();lost.append(result.json());route.abort('failed')
    page.route('**/dex-gas-returns/*/submit',intercept)
    recovery.get_by_role('button',name='Confirmar devolución',exact=True).click()
    expect(recovery.locator('[data-return-status]')).to_contain_text('Devolución sin confirmar')
    expect(recovery.get_by_role('button',name='Confirmar devolución',exact=True)).not_to_be_visible()
    recovery.get_by_role('button',name='Actualizar devoluciones',exact=True).click()
    card=recovery.locator('[data-gas-return-id="'+record['id']+'"]')
    expect(card).to_contain_text('BNB recuperado',timeout=15000)
    assert len(lost)==1 and lost[0]['status']=='FINALIZED'
    state=page.request.get(BASE+'/__test/state').json()
    assert state['sends']==2 and state['native_returns']==1 and state['wallet_sends']==0 and state['operator_balance_wei']=='0'
    assert int(state['owner_balance_wei'])-int(before['owner_balance_wei'])==int(record['amount_wei'])
    recovery.screenshot(path=str(output/'return-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    recovery.screenshot(path=str(output/'return-mobile.png'))
    page.locator('#real-lock').click();parent=unlock(page)
    recovery=parent.locator('[data-dex-return]');recovery.locator(':scope > details > summary').click()
    expect(recovery.locator('[data-return-history]')).to_contain_text('BNB recuperado')
    assert page.request.get(BASE+'/__test/state').json()['native_returns']==1
    assert page.evaluate("!(window.fixtureMethods||[]).includes('eth_sendTransaction')&&!(window.fixtureMethods||[]).includes('eth_sendRawTransaction')")
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and not errors,errors
    (output/'return-result.json').write_text(json.dumps({'passed':True,'swap_sends':2,'native_returns':1,'wallet_sends':0,
        'actual_owner_balance_delta_wei':record['amount_wei'],'gas_wei':str(21000*10**9),
        'lost_submit_response_recovered_by_get':True,'reopen_without_autorun':True,'errors':errors},indent=2))
    browser.close();print('DEX buy/sell, STOP, one exact BNB return, lost response recovery and locked reopen')
