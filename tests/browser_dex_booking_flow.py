"""DEX buy/sell → wallet accounting → lost response recovery → chart/book reopen."""
import json,os
from decimal import Decimal
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];records=[];page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name and fixture['booking_fixture']
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD);operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[name="dex_run_binding"]')).to_have_value(fixture['binding_id'])
    runs.locator('[name="dex_run_permission"]').check();runs.get_by_role('button',name='Activar',exact=True).click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Activo',timeout=15000)
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    expect(runs.locator('[data-run-history]')).to_contain_text('Compra · Confirmada',timeout=20000)
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'exit'})
    expect(runs.locator('[data-run-history]')).to_contain_text('Venta · Confirmada',timeout=20000)
    runs.get_by_role('button',name='Detener',exact=True).click();expect(runs.locator('[data-run-history]')).to_contain_text('Detenido')
    accounting=parent.locator('[data-dex-booking]');accounting.locator('summary').click()
    expect(accounting.locator('[data-book-history]')).to_contain_text('Compra · Pendiente')
    assert page.request.get(BASE+'/__test/book').json()['events']==2
    if fixture['order_proof_fixture']:
        # This real HTTP check discovers signed transactions from our own Anvil
        # block. A lost reply must not display an unconfirmed proof or retry.
        status=page.request.get(BASE+'/real/vault/status')
        assert status.json()['capabilities']['dex_receipt_order_proof'] is True
        proof_card=accounting.locator('[data-book-kind="OPERATOR"]').nth(0)
        saved=[]
        def lost_order(route):
            result=route.fetch();assert result.status==200,result.text();saved.append(result.json());route.abort('failed')
        page.route('**/dex-receipts/OPERATOR/*/order-proof',lost_order)
        proof_card.get_by_role('button',name='Comprobar bloque',exact=True).click()
        expect(accounting.locator('[data-book-status]')).to_contain_text('Bloque sin confirmar')
        expect(proof_card.locator('[data-book-order-saved]')).to_have_count(0)
        assert len(saved)==1 and saved[0]['created'] is True
        before_proofs=page.request.get(BASE+'/__test/book').json()
        assert before_proofs['events']==2 and before_proofs['order_proofs']==1
        page.unroute('**/dex-receipts/OPERATOR/*/order-proof')
        with page.expect_response(lambda r:r.url.endswith('/order-proof')) as response:
            proof_card.get_by_role('button',name='Comprobar bloque',exact=True).click()
        assert response.value.status==200 and response.value.json()['created'] is False
        expect(proof_card.locator('[data-book-order-saved]')).to_have_text('Bloque ✓')
        accounting.locator('[data-book-kind="OPERATOR"]').nth(1).get_by_role('button',name='Comprobar bloque',exact=True).click()
        expect(accounting.locator('[data-book-order-saved]')).to_have_count(2)
        after_proofs=page.request.get(BASE+'/__test/book').json()
        assert after_proofs['order_proofs']==2 and after_proofs['events']==2 and after_proofs['bookings']==0
    accounting.locator('[data-book-kind="OPERATOR"]').nth(1).get_by_role('button',name='Revisar',exact=True).click()
    expect(accounting.locator('[data-book-status]')).to_contain_text('Contabiliza primero')
    for index in range(2):
        cards=accounting.locator('[data-book-kind="OPERATOR"]');card=cards.nth(index)
        card.get_by_role('button',name='Revisar',exact=True).click()
        expect(accounting.locator('[data-book-preview]')).to_contain_text('Wallet BNB')
        expect(accounting.get_by_role('button',name='Contabilizar',exact=True)).to_be_enabled()
        if index==0:
            def lost(route):
                result=route.fetch();assert result.status==200,result.text();records.append(result.json());route.abort('failed')
            page.route('**/dex-receipts/OPERATOR/*/book',lost)
            accounting.get_by_role('button',name='Contabilizar',exact=True).click()
            expect(accounting.locator('[data-book-status]')).to_contain_text('Registro sin confirmar')
            expect(accounting.get_by_role('button',name='Contabilizar',exact=True)).not_to_be_visible()
            page.unroute('**/dex-receipts/OPERATOR/*/book')
            accounting.get_by_role('button',name='Actualizar recibos',exact=True).click()
            expect(accounting.locator('[data-book-kind="OPERATOR"]').nth(0)).to_contain_text('Contabilizado')
        else:
            with page.expect_response(lambda r:'/dex-receipts/OPERATOR/' in r.url and r.url.endswith('/book')) as response:
                accounting.get_by_role('button',name='Contabilizar',exact=True).click()
            assert response.value.status==200,response.value.text();records.append(response.value.json())
            expect(page.locator('#real-history')).to_contain_text('SELL')
    assert len(records)==2 and all(item['events_created']==2 for item in records)
    ledger=page.request.get(BASE+'/__test/book').json();assert ledger['bookings']==2 and ledger['events']==6
    assert ledger['contributions']=={'USDT':'100','BNB':'0.01'} and Decimal(ledger['positions']['WBNB'])==0
    assert Decimal(ledger['cash']['USDT'])==100 and Decimal(ledger['cash']['BNB'])<Decimal('0.01')
    # Global portfolio refresh may rebuild its view after a confirmed booking.
    if not parent.locator(':scope > details').evaluate('el=>el.open'):
        page.get_by_role('button',name='Verificar control',exact=True).click()
        expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
        parent.locator(':scope > details > summary').click()
    accounting=parent.locator('[data-dex-booking]')
    if not accounting.locator('details').evaluate('el=>el.open'):accounting.locator('summary').click()
    expect(accounting.locator('[data-book-history]')).to_contain_text('Compra · Contabilizado')
    # Book one funding fee: transferring the principal is not a contribution.
    funding=accounting.locator('[data-book-kind="GAS_FUNDING"]');funding.get_by_role('button',name='Revisar',exact=True).click()
    expect(accounting.locator('[data-book-preview]')).to_contain_text('Principal · Traslado interno')
    with page.expect_response(lambda r:'/dex-receipts/GAS_FUNDING/' in r.url and r.url.endswith('/book')) as response:
        accounting.get_by_role('button',name='Contabilizar',exact=True).click()
    assert response.value.status==200 and response.value.json()['events_created']==1
    after=page.request.get(BASE+'/__test/book').json();assert after['contributions']==ledger['contributions'] and after['cash']['USDT']==ledger['cash']['USDT'] and after['events']==7
    page.locator('#real-lock').click();parent=unlock(page)
    accounting=parent.locator('[data-dex-booking]');accounting.locator('summary').click()
    expect(accounting.locator('[data-book-kind="OPERATOR"]').nth(0)).to_contain_text('Contabilizado')
    expect(accounting.locator('[data-book-kind="GAS_FUNDING"]')).to_contain_text('Contabilizado')
    if fixture['order_proof_fixture']:
        card=accounting.locator('[data-book-kind="OPERATOR"]').nth(0)
        with page.expect_response(lambda r:r.url.endswith('/order-proof')) as response:
            card.get_by_role('button',name='Comprobar bloque',exact=True).click()
        assert response.value.status==200 and response.value.json()['created'] is False
        expect(card.locator('[data-book-order-saved]')).to_have_text('Bloque ✓')
    accounting.screenshot(path=str(output/'booking-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    accounting.screenshot(path=str(output/'booking-mobile.png'))
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    runs.get_by_role('button',name='Gráfico',exact=True).click()
    graph=runs.locator('[data-dex-chart]');expect(graph.locator('[data-real-chart-canvas]')).to_have_attribute('data-fill-count','2')
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('Asiento')
    expect(graph.locator('[data-real-chart-inspect]')).not_to_contain_text('Pendiente de contabilizar')
    with page.expect_download() as downloaded:
        graph.get_by_role('button',name='Exportar JSONL',exact=True).click()
    raw=downloaded.value.path().read_text()
    rows=[json.loads(line) for line in raw.splitlines()]
    assert len(rows)==2 and all(row['swap']['financially_booked'] for row in rows)
    assert all('event_id' not in row['swap'] and 'booking_id' not in row['swap'] for row in rows)
    downloaded.value.save_as(output/'uniswap-booked-verified.jsonl')
    assert page.request.get(BASE+'/__test/state').json()['sends']==2
    assert not errors,errors
    (output/'booking-result.json').write_text(json.dumps({'passed':True,'swaps':2,'bookings':3,'ledger_events':7,
        'lost_response_recovered_without_post_retry':True,'order_proof_fixture':fixture['order_proof_fixture'],
        'principal_not_contribution':True,'chart_links':True,'research_export_booked':True,'errors':errors},indent=2))
    browser.close();print('DEX buy/sell and native gas accounting, exact wallet book, lost response recovery, links, mobile and reopen')
