"""Unknown send → explicit higher cancellation → lost reply → reopen and return."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.browser_network import route_once
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name and fixture['cancellation_fixture']
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    def restore(parent):
        owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
        operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
        operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
        operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
        operator.get_by_role('button',name='Restaurar copia',exact=True).click()
        expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    restore(parent)
    recovery=parent.locator('[data-dex-return]');recovery.locator(':scope > details > summary').click()
    original=recovery.locator('[data-gas-return-id="'+fixture['return_id']+'"]')
    expect(original).to_contain_text('Recibo pendiente')
    original.get_by_role('button',name='Cancelar devolución',exact=True).click()
    panel=recovery.locator('[data-return-cancellation]');expect(panel).to_be_visible()
    def prepare():
        with page.expect_response(lambda response:response.url.endswith('/dex-gas-cancellations') and response.request.method=='POST') as pending:
            panel.get_by_role('button',name='Revisar cancelación',exact=True).click()
        response=pending.value;assert response.status==200,response.text();return response.json()
    panel.locator('[name="dex_cancel_price"]').fill('1.2')
    panel.locator('[name="dex_cancel_bump"]').fill('15')
    panel.locator('[name="dex_cancel_maximum"]').fill('0.00008')
    first=prepare();expect(panel.locator('[data-cancel-preview]')).to_contain_text('Coste')
    panel.locator('[name="dex_cancel_price"]').fill('1.1')
    prepared_card=panel.locator('[data-gas-cancellation-id="'+first['id']+'"]')
    prepared_card.get_by_role('button',name='Recuperar cancelación',exact=True).click()
    expect(panel.locator('[name="dex_cancel_price"]')).to_have_value('1.2')
    expect(panel.locator('[name="dex_cancel_bump"]')).to_have_value('15')
    expect(panel.locator('[name="dex_cancel_maximum"]')).to_have_value('0.00008')
    panel.get_by_role('button',name='Confirmar cancelación',exact=True).click()
    first_card=panel.locator('[data-gas-cancellation-id="'+first['id']+'"]')
    expect(first_card).to_contain_text('Cancelación pendiente',timeout=15000)
    assert page.request.get(BASE+'/__test/state').json()['cancel_posts']==1
    first_card.get_by_role('button',name='Revisar otra cancelación',exact=True).click()
    second=prepare();assert second['previous_posts']==1 and second['base_gas_price']==first['gas_price']
    assert second['gas_price']>first['gas_price'] and second['nonce']==first['nonce']
    lost=[]
    def lose(route):
        response=route.fetch();assert response.status==200,response.text();lost.append(response.json());route.abort('failed')
    route_once(page,'**/dex-gas-cancellations/*/submit',lose)
    panel.get_by_role('button',name='Confirmar cancelación',exact=True).click()
    expect(panel.locator('[data-cancel-status]')).to_contain_text('Sin confirmar')
    expect(panel.get_by_role('button',name='Confirmar cancelación',exact=True)).not_to_be_visible()
    recovery.get_by_role('button',name='Actualizar devoluciones',exact=True).click()
    second_card=panel.locator('[data-gas-cancellation-id="'+second['id']+'"]')
    expect(second_card).to_contain_text('Cancelación confirmada')
    expect(original).to_contain_text('Cancelado · Resuelto')
    expect(panel.locator('[data-cancel-status]')).not_to_contain_text('Sin confirmar')
    first_card.get_by_role('button',name='Comprobar cancelación',exact=True).click()
    expect(first_card).to_contain_text('Resuelta · Otra cancelación')
    expect(first_card).not_to_contain_text('Gas ganador')
    assert len(lost)==1 and lost[0]['winner_id']==second['id']
    assert page.request.get(BASE+'/__test/state').json()['cancel_posts']==2
    recovery.screenshot(path=str(output/'cancellation-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+1')
    recovery.screenshot(path=str(output/'cancellation-mobile.png'))
    page.locator('#real-lock').click();parent=unlock(page);restore(parent)
    recovery=parent.locator('[data-dex-return]');recovery.locator(':scope > details > summary').click()
    expect(recovery.locator('[data-cancel-history]')).to_contain_text('Cancelación confirmada')
    assert page.request.get(BASE+'/__test/state').json()['cancel_posts']==2
    recovery.get_by_role('button',name='Revisar devolución',exact=True).click()
    expect(recovery.locator('[data-return-preview]')).to_contain_text('Recuperable')
    recovery.get_by_role('button',name='Confirmar devolución',exact=True).click()
    expect(recovery.locator('[data-return-history]')).to_contain_text('BNB recuperado',timeout=15000)
    assert page.request.get(BASE+'/__test/state').json()['operator_balance_wei']=='0'
    booking=parent.locator('[data-dex-booking]');booking.locator(':scope > details > summary').click()
    native=booking.locator('[data-book-kind="NATIVE_RESOLUTION"]');expect(native).to_have_count(1)
    native.get_by_role('button',name='Revisar',exact=True).click();booking.get_by_role('button',name='Contabilizar',exact=True).click()
    expect(native.get_by_role('button',name='Revisar',exact=True)).to_have_count(0)
    assert not errors,errors
    methods=page.evaluate('window.fixtureMethods||[]');assert 'eth_sendTransaction' not in methods and 'eth_sendRawTransaction' not in methods
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and fixture['original_hash'] not in storage and lost[0]['tx_hash'] not in storage
    (output/'cancellation-result.json').write_text(json.dumps({'passed':True,'no_arrival_then_explicit_higher_request':True,
        'lost_reply_recovered_by_get':True,'one_winning_cancellation_booking':True,'reopen_without_autorun':True,
        'next_nonce_owner_return':True,'cancel_posts':2,'errors':errors},indent=2))
    browser.close();print('Pending cancellation, explicit higher mandate, lost reply, reopen, next-nonce return and one fee passed')
