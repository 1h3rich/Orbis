"""Actual owned-wallet replacement, missing replies, remount, receipt and funding."""
import json,os,re
from pathlib import Path
from decimal import Decimal
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import PROVIDER,unlock
from tests.browser_network import route_once
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050});errors=[];reports=[]
    page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    page.on('request',lambda request:reports.append(request.url) if re.search(r'/dex-wallet-cancellations/[^/]+/(hash|observe)$',request.url) else None)
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name and fixture['wallet_cancellation_fixture']
    page.expose_function('fixtureSign',lambda message:'0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('11'*32)).signature.hex())
    provider=PROVIDER.replace('WALLET',json.dumps(fixture['wallet'])).replace('FIXTURE',json.dumps(fixture['fixture_id'])).replace('ALLOW_SEND','true')
    provider=provider.replace("return (await response.json()).result;", "const value=await response.json();if(value.error){const error=new Error(value.error.message);error.code=value.error.code;throw error;}return value.result;")
    page.add_init_script(provider);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    def control(method):
        response=page.request.post(BASE+'/__test/control',data={'fixture_id':fixture['fixture_id'],'method':method,'params':[]})
        assert response.ok,response.text()
    def gas_panel(parent):
        gas=parent.locator('[data-dex-gas]');gas.locator(':scope > details > summary').click()
        gas.get_by_text('Envíos de gas',exact=True).click();return gas
    # Static assets can be refreshed before the authorized backend restart.
    # A missing new endpoint must leave existing gas history working.
    route_once(page,'**/dex-wallet-cancellations',lambda route:route.fulfill(status=404,content_type='application/json',body='{"detail":"Not Found"}'))
    gas=gas_panel(parent);source=gas.locator('[data-gas-funding-id="'+fixture['funding_id']+'"]')
    expect(source).to_contain_text('Respuesta pendiente')
    expect(gas.locator('[data-gas-status]')).to_contain_text('reinicio pendiente')
    expect(source.get_by_role('button',name='Cancelar financiación',exact=True)).to_have_count(0)
    gas.get_by_role('button',name='Actualizar gas',exact=True).click();expect(source.get_by_role('button',name='Cancelar financiación',exact=True)).to_be_enabled()
    source.get_by_role('textbox',name=re.compile('Hash del envío')).fill(fixture['original_hash'])
    source.get_by_role('button',name='Cancelar financiación',exact=True).click()
    panel=gas.locator('[data-funding-cancellation]');expect(panel.locator('[name="dex_wallet_cancel_recipient"]')).to_have_value(fixture['operator'])
    def prepare():
        with page.expect_response(lambda response:response.url.endswith('/dex-wallet-cancellations') and response.request.method=='POST') as pending:
            panel.get_by_role('button',name='Revisar cancelación',exact=True).click()
        response=pending.value;assert response.status==200,response.text();return response.json()
    panel.locator('[name="dex_wallet_cancel_price"]').fill('1.2');panel.locator('[name="dex_wallet_cancel_bump"]').fill('15');panel.locator('[name="dex_wallet_cancel_maximum"]').fill('0.00008')
    first=prepare();panel.locator('[name="dex_wallet_cancel_price"]').fill('1.1')
    card=panel.locator('[data-wallet-cancellation-id="'+first['id']+'"]');card.get_by_role('button',name='Recuperar cancelación',exact=True).click()
    expect(panel.locator('[name="dex_wallet_cancel_price"]')).to_have_value('1.2')
    expect(panel.locator('[name="dex_wallet_cancel_bump"]')).to_have_value('15')
    expect(panel.locator('[name="dex_wallet_cancel_maximum"]')).to_have_value('0.00008')
    lost=[]
    def lose(route):
        response=route.fetch();assert response.status==200,response.text();lost.append(response.json());route.abort('failed')
    route_once(page,'**/dex-wallet-cancellations/*/claim',lose)
    panel.get_by_role('button',name='Confirmar cancelación en wallet',exact=True).click()
    expect(panel.locator('[data-wallet-cancel-status]')).to_contain_text('Concesión sin confirmar')
    expect(panel.get_by_role('button',name='Confirmar cancelación en wallet',exact=True)).not_to_be_visible()
    assert len(lost)==1 and lost[0]['claim_granted'] and page.request.get(BASE+'/__test/state').json()['wallet_calls']==0
    gas.get_by_role('button',name='Actualizar gas',exact=True).click();expect(card).to_contain_text('Resultado pendiente')
    expect(panel.locator('[data-wallet-cancel-status]')).not_to_contain_text('sin confirmar')
    card.get_by_role('button',name='Revisar otra cancelación',exact=True).click()
    panel.locator('[name="dex_wallet_cancel_price"]').fill('1.5');panel.locator('[name="dex_wallet_cancel_unknown"]').check()
    second=prepare();assert second['previous_prices']==[1200000000] and second['previous_fees_unknown']
    control('HOLD_MINED')
    panel.get_by_role('button',name='Confirmar cancelación en wallet',exact=True).click()
    # The actual cancellation is mined, but its wallet response remains pending.
    # The 30s UI timeout must not reset the pending provider promise.
    expect(panel.locator('[data-wallet-cancel-status]')).to_contain_text('Wallet sin respuesta',timeout=35000)
    state=page.request.get(BASE+'/__test/state').json();assert state['wallet_calls']==state['cancel_sends']==1
    expect(source.get_by_role('button',name='Cancelar financiación',exact=True)).to_be_disabled()
    before_reports=len(reports)
    page.locator('.navegacion-principal a[href="#/estudio"]').click();page.locator('.navegacion-principal a[href="#/real"]').click()
    expect(page.locator('#real-account')).to_be_visible()
    if page.get_by_role('button',name='Conectar wallet',exact=True).is_visible():page.get_by_role('button',name='Conectar wallet',exact=True).click()
    else:page.locator('#real-wallet-refresh').click()
    page.get_by_role('button',name='Verificar control',exact=True).click();expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    parent=page.locator('#real-wallet-dex-bot');parent.locator(':scope > details > summary').click();gas=gas_panel(parent)
    # Global pending state survives the component being recreated in a new DOM.
    expect(gas.get_by_role('button',name='Actualizar gas',exact=True)).to_be_disabled()
    assert page.request.get(BASE+'/__test/state').json()['wallet_calls']==1
    control('release');expect(gas.get_by_role('button',name='Actualizar gas',exact=True)).to_be_enabled(timeout=10000)
    assert len(reports)==before_reports,'Late old-context wallet response must not report into new session'
    gas.get_by_role('button',name='Actualizar gas',exact=True).click();panel=gas.locator('[data-funding-cancellation]')
    second_card=panel.locator('[data-wallet-cancellation-id="'+second['id']+'"]');expect(second_card).to_contain_text('Resultado pendiente')
    second_card.get_by_role('textbox',name=re.compile('Hash de cancelación')).fill(state['cancel_hashes'][0])
    second_card.get_by_role('button',name='Comprobar cancelación',exact=True).click();expect(second_card).to_contain_text('Cancelación confirmada')
    first_card=panel.locator('[data-wallet-cancellation-id="'+first['id']+'"]');first_card.get_by_role('button',name='Vincular resultado',exact=True).click()
    expect(first_card).to_contain_text('Resuelta · Otra cancelación');expect(first_card).not_to_contain_text('Gas ganador')
    assert page.request.get(BASE+'/__test/state').json()['wallet_calls']==1
    # A new reviewed funding uses the next nonce, from the same wallet and book.
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json');operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click();expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    control('FUND_PENDING');gas.locator('[name="dex_gas_amount"]').fill('0.0005')
    with page.expect_response(lambda response:response.url.endswith('/dex-gas-funding') and response.request.method=='POST') as pending:
        gas.get_by_role('button',name='Preparar gas',exact=True).click()
    response=pending.value;assert response.status==200,response.text();fresh=response.json();assert int(fresh['nonce'])==int(first['nonce'])+1
    gas.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    fresh_card=gas.locator('[data-gas-funding-id="'+fresh['id']+'"]');expect(fresh_card).to_contain_text('Recibo pendiente')
    fresh_card.get_by_role('button',name='Cancelar financiación',exact=True).click();third=prepare()
    control('REJECT');panel.get_by_role('button',name='Confirmar cancelación en wallet',exact=True).click()
    expect(panel.locator('[data-wallet-cancellation-id="'+third['id']+'"]')).to_contain_text('Rechazada en wallet')
    fresh_card.get_by_role('button',name='Cancelar financiación',exact=True).click();panel.locator('[name="dex_wallet_cancel_unknown"]').check();fourth=prepare()
    before=page.request.get(BASE+'/__test/state').json();assert before['wallet_calls']==3 and before['funding_sends']==1
    control('mine_original');panel.get_by_role('button',name='Confirmar cancelación en wallet',exact=True).click()
    expect(panel.locator('[data-wallet-cancel-status]')).to_contain_text('minado')
    assert page.request.get(BASE+'/__test/state').json()['wallet_calls']==3
    fresh_card.get_by_role('button',name='Comprobar envío',exact=True).click();expect(fresh_card).to_contain_text('BNB confirmado')
    gas.get_by_role('button',name='Actualizar gas',exact=True).click()
    fourth_card=panel.locator('[data-wallet-cancellation-id="'+fourth['id']+'"]');fourth_card.get_by_role('button',name='Vincular resultado',exact=True).click()
    expect(fourth_card).to_contain_text('Financiación confirmada')
    gas.screenshot(path=str(output/'wallet-cancel-desktop.png'));page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+1');gas.screenshot(path=str(output/'wallet-cancel-mobile.png'))
    booking=parent.locator('[data-dex-booking]');booking.locator(':scope > details > summary').click()
    native=booking.locator('[data-book-kind="NATIVE_RESOLUTION"],[data-book-kind="GAS_FUNDING"]');expect(native).to_have_count(2)
    for remaining in (2,1):
        buttons=native.get_by_role('button',name='Revisar',exact=True);expect(buttons).to_have_count(remaining);buttons.first.click()
        booking.get_by_role('button',name='Contabilizar',exact=True).click();expect(buttons).to_have_count(remaining-1)
    final=page.request.get(BASE+'/__test/state').json()
    assert final['wallet_calls']==3 and final['cancel_sends']==1 and final['funding_sends']==1
    assert Decimal(final['total_balance_loss_wei'])==sum(map(Decimal,final['fees_wei']))
    page.locator('#real-lock').click();parent=unlock(page);gas=gas_panel(parent)
    expect(gas.locator('[data-wallet-cancel-history]')).to_contain_text('Cancelación confirmada')
    expect(gas.locator('[data-wallet-cancel-history]')).to_contain_text('Financiación confirmada')
    assert page.request.get(BASE+'/__test/state').json()['wallet_calls']==3
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    assert BACKUP_PASSWORD not in storage and fixture['original_hash'] not in storage and state['cancel_hashes'][0] not in storage
    assert not errors,errors
    (output/'wallet-cancel-result.json').write_text(json.dumps({'passed':True,'lost_claim_no_wallet_send':True,
        'older_backend_preserves_existing_gas_ui':True,
        'held_wallet_promise_survives_timeout_and_remount':True,'late_result_not_reported_to_new_context':True,
        'one_actual_cancellation':True,'one_next_nonce_funding':True,'original_winner_before_claim':True,
        'wallet_rejection':True,'two_unique_native_fee_bookings':True,'wallet_calls':3,'errors':errors},indent=2))
    browser.close();print('Wallet cancellation, claim loss, held reply/remount, rejection, original winner, next nonce and unique fees passed')
