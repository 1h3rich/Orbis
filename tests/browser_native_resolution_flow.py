"""Cancel native funding/return, reconcile gas, then finish return. Own EVM only."""
import json,os
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import PROVIDER,unlock
from tests.browser_network import route_once
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name and fixture['native_fixture']
    page.expose_function('fixtureSign',lambda message:'0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('11'*32)).signature.hex())
    page.add_init_script(PROVIDER.replace('WALLET',json.dumps(fixture['wallet'])).replace('FIXTURE',json.dumps(fixture['fixture_id'])).replace('ALLOW_SEND','true'))
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    def restore(parent):
        owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
        operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
        operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
        operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
        operator.get_by_role('button',name='Restaurar copia',exact=True).click()
        expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    restore(parent)
    gas=parent.locator('[data-dex-gas]');gas.locator(':scope > details > summary').click()
    expect(gas.locator('[name="dex_gas_binding"]')).to_have_value(fixture['binding_id'])
    def prepare():
        gas.locator('[name="dex_gas_amount"]').fill('0.0005');gas.get_by_role('button',name='Preparar gas',exact=True).click()
        expect(gas.locator('[data-gas-send]')).to_be_enabled()
    prepare();route_once(page,'**/dex-gas-funding/*/hash',lambda route:route.abort('failed'))
    gas.locator('[data-gas-send]').click();expect(gas.locator('[data-gas-history]')).to_contain_text('Respuesta pendiente')
    gas.get_by_text('Envíos de gas',exact=True).click()
    first=page.request.get(BASE+'/__test/state').json();assert first['sends']==1 and first['operator_balance']=='0'
    gas.get_by_role('textbox',name='Hash del envío',exact=False).fill('')
    with page.expect_response('**/dex-native-resolutions/GAS_FUNDING/*/discover') as funding_search:
        gas.get_by_role('button',name='Revisar reemplazo',exact=True).click()
    assert funding_search.value.status==200 and funding_search.value.json()['discovered_tx_hash']==first['funding_cancel_hash']
    review=gas.locator('[data-gas-resolution]');expect(review).to_contain_text('Cancelación confirmada')
    lost=[]
    def lose(route):
        response=route.fetch();assert response.status==200,response.text();lost.append(response.json());route.abort('failed')
    route_once(page,'**/dex-native-resolutions/GAS_FUNDING/*/resolve',lose)
    review.get_by_role('button',name='Confirmar resolución',exact=True).click();expect(review).to_contain_text('Resolución sin confirmar')
    gas.get_by_role('button',name='Actualizar gas',exact=True).click()
    expect(gas.locator('[data-gas-history]')).to_contain_text('Cancelado · Resuelto')
    expect(review).not_to_contain_text('sin confirmar')
    assert len(lost)==1 and not gas.get_by_role('button',name='Revisar reemplazo',exact=True).count()
    prepare();gas.locator('[data-gas-send]').click();expect(gas.locator('[data-gas-history]')).to_contain_text('BNB confirmado')
    assert page.request.get(BASE+'/__test/state').json()['operator_balance']==str(5*10**14)
    recovery=parent.locator('[data-dex-return]');recovery.locator(':scope > details > summary').click()
    expect(recovery.locator('[name="dex_return_binding"]')).to_have_value(fixture['binding_id'])
    recovery.get_by_role('button',name='Revisar devolución',exact=True).click()
    recovery.get_by_role('button',name='Confirmar devolución',exact=True).click()
    expect(recovery.locator('[data-return-history]')).to_contain_text('Recibo pendiente')
    tx=page.request.post(BASE+'/__test/native-cancel',data={'fixture_id':fixture['fixture_id'],'method':'cancel_return','params':[]}).json()['hash']
    page.locator('#real-lock').click();parent=unlock(page)
    recovery=parent.locator('[data-dex-return]');recovery.locator(':scope > details > summary').click()
    with page.expect_response('**/dex-native-resolutions/GAS_RETURN/*/discover') as return_search:
        recovery.get_by_role('button',name='Revisar reemplazo',exact=True).click()
    assert return_search.value.status==200 and return_search.value.json()['discovered_tx_hash']==tx
    expect(recovery.locator('[data-return-resolution]')).to_contain_text('Cancelación confirmada')
    recovery.get_by_role('button',name='Confirmar resolución',exact=True).click()
    expect(recovery.locator('[data-return-history]')).to_contain_text('Cancelado · Resuelto')
    recovery.screenshot(path=str(output/'native-resolution-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+1')
    recovery.screenshot(path=str(output/'native-resolution-mobile.png'))
    page.locator('#real-lock').click();parent=unlock(page);restore(parent)
    recovery=parent.locator('[data-dex-return]');recovery.locator(':scope > details > summary').click()
    expect(recovery.locator('[data-return-history]')).to_contain_text('Cancelado · Resuelto')
    recovery.get_by_role('button',name='Revisar devolución',exact=True).click()
    recovery.get_by_role('button',name='Confirmar devolución',exact=True).click()
    expect(recovery.locator('[data-return-history]')).to_contain_text('BNB recuperado')
    final=page.request.get(BASE+'/__test/state').json()
    assert final['operator_balance']=='0' and final['sends']==2 and final['return_attempts']==2 and final['native_returns']==1
    booking=parent.locator('[data-dex-booking]');booking.locator(':scope > details > summary').click()
    expect(booking.locator('[data-book-history]')).to_contain_text('Cancelación')
    native_cards=booking.locator('[data-book-kind="NATIVE_RESOLUTION"],[data-book-kind="GAS_FUNDING"],[data-book-kind="GAS_RETURN"]')
    expect(native_cards).to_have_count(4)
    for remaining in range(4,0,-1):
        buttons=native_cards.get_by_role('button',name='Revisar',exact=True)
        expect(buttons).to_have_count(remaining)
        buttons.first.click()
        booking.get_by_role('button',name='Contabilizar',exact=True).click()
        expect(buttons).to_have_count(remaining-1)
    expect(native_cards.get_by_role('button',name='Revisar',exact=True)).to_have_count(0)
    assert not errors,errors
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and tx not in storage and first['funding_cancel_hash'] not in storage
    (output/'native-result.json').write_text(json.dumps({'passed':True,'funding_cancellation':True,'return_cancellation':True,
        'found_both_hashes_in_bnb':True,'return_discovered_without_operator_key':True,
        'lost_response_recovered_without_post_retry':True,'reopen_next_nonce_return':True,'wallet_sends':2,'native_returns':1,'bookings':4,'errors':errors},indent=2))
    browser.close();print('Native funding/return cancellation, lost response, reopen, next-nonce return and four gas bookings passed')
