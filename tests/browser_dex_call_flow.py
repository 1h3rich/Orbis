"""Real recovered call -> concise review -> wallet accounting; no browser send."""
import json,os,time
from pathlib import Path
from decimal import Decimal
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.browser_network import route_once

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];calls=[];lost=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('request',lambda request:calls.append((request.method,request.url)))
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    (output/'initial-dom.yml').write_text(page.locator('body').aria_snapshot())
    unlock(page)
    def controls():
        if fixture['kind']=='OPERATOR':
            host=page.locator('#real-wallet-dex-bot [data-dex-runs]')
            host.locator(':scope > details > summary').click()
            row=host.locator('[data-dex-intent-id="'+fixture['source_id']+'"]')
            return host,row,host.locator('[data-run-resolution]')
        host=page.locator('#real-wallet-swap');host.locator(':scope > details > summary').click()
        host.locator('summary',has_text='Historial DEX').click()
        row=host.locator('[data-swap-action="'+fixture['source_id']+'"]')
        return host,row,host.locator('[data-swap-resolution]')
    host,row,panel=controls()
    expect(row.get_by_role('button',name='Revisar resultado',exact=True)).to_be_visible()
    if fixture['kind']=='OPERATOR':
        expect(row.get_by_role('button',name='Revisar resultado',exact=True)).to_be_disabled()
        host.get_by_role('button',name='Detener',exact=True).click()
        expect(row.get_by_role('button',name='Revisar resultado',exact=True)).to_be_enabled()
    def review():
        expect(row.get_by_role('textbox')).to_have_value('')
        with page.expect_response('**/dex-call-resolutions/*/*/discover') as located:
            row.get_by_role('button',name='Revisar resultado',exact=True).click()
        assert located.value.status==200 and located.value.json()['discovered_tx_hash']==fixture['tx_hash'],located.value.text()
        expect(panel).to_contain_text('Operación confirmada' if fixture['outcome']=='EXECUTED' else 'Cancelación confirmada')
        expect(panel.get_by_role('button',name='Confirmar resolución',exact=True)).to_be_disabled()
        panel.get_by_role('checkbox',name='Aceptar cambios de llamada y gas',exact=True).check()
        expect(panel.get_by_role('button',name='Confirmar resolución',exact=True)).to_be_enabled()
    review()
    panel.screenshot(path=str(output/'call-review.png'))
    if fixture['kind']=='WALLET_SWAP' and fixture['outcome']=='EXECUTED':
        page.request.post(BASE+'/__test/control',data={'fixture_id':fixture['fixture_id'],'method':'hold'})
        panel.get_by_role('button',name='Confirmar resolución',exact=True).click()
        deadline=time.monotonic()+5
        while not page.request.get(BASE+'/__test/state').json()['held']:
            assert time.monotonic()<deadline;page.wait_for_timeout(50)
        page.locator('#real-lock').click();expect(page.locator('#real-passphrase')).to_be_visible()
        page.request.post(BASE+'/__test/control',data={'fixture_id':fixture['fixture_id'],'method':'release'})
        unlock(page);host,row,panel=controls()
        assert page.request.get(BASE+'/__test/state').json()['resolved'] is False
        review()
    def lose(route):
        response=route.fetch();assert response.status==200,response.text();lost.append(response.json());route.abort('failed')
    route_once(page,'**/dex-call-resolutions/*/*/resolve',lose)
    before=sum(method=='POST' and url.endswith('/resolve') for method,url in calls)
    panel.get_by_role('button',name='Confirmar resolución',exact=True).click()
    expect(row).to_contain_text('Confirmada' if fixture['outcome']=='EXECUTED' else 'Cancelada')
    expect(row.get_by_role('button',name='Revisar resultado',exact=True)).not_to_be_visible()
    assert len(lost)==1 and lost[0]['resolved']
    assert sum(method=='POST' and url.endswith('/resolve') for method,url in calls)==before+1
    assert any(method=='GET' and '/dex-call-resolutions/' in url and url.endswith('/resolution') for method,url in calls)
    assert all(fixture['tx_hash'] not in url and fixture['wallet'].lower() not in url.lower() for _,url in calls)
    if fixture['outcome']=='EXECUTED' and fixture['kind']=='WALLET_SWAP':
        with page.expect_response(lambda r:r.url.endswith('/order-proof')) as response:
            row.get_by_role('button',name='Comprobar bloque',exact=True).click()
        assert response.value.status==200 and response.value.json()['created'] is True
        expect(row.locator('[data-swap-order-saved]')).to_have_text('Bloque ✓')
        assert page.request.get(BASE+'/__test/book').json()['events']==2
    elif fixture['outcome']=='CANCELLED' and fixture['kind']=='WALLET_SWAP':
        expect(row.get_by_role('button',name='Comprobar bloque',exact=True)).to_have_count(0)
    if fixture['kind']=='WALLET_SWAP':
        row.get_by_role('button',name='Revisar registro',exact=True).click()
        book_panel=host.locator('[data-swap-booking]')
        expect(book_panel).to_be_visible()
        if fixture['outcome']=='CANCELLED':expect(book_panel).to_contain_text('Sólo comisión')
        book_panel.get_by_role('button',name='Registrar',exact=True).click()
        expect(row).to_contain_text('contabilizado')
    else:
        ledger=page.locator('#real-wallet-dex-bot [data-dex-booking]');ledger.locator(':scope > details > summary').click()
        receipt=ledger.locator('[data-book-kind="CALL_RESOLUTION"]')
        if fixture['outcome']=='EXECUTED':
            receipt.get_by_role('button',name='Comprobar bloque',exact=True).click()
            expect(receipt.locator('[data-book-order-saved]')).to_have_text('Bloque ✓')
        else:expect(receipt.get_by_role('button',name='Comprobar bloque',exact=True)).to_have_count(0)
        expect(receipt).to_be_visible();receipt.get_by_role('button',name='Revisar',exact=True).click()
        ledger.get_by_role('button',name='Contabilizar',exact=True).click();expect(receipt).to_contain_text('Contabilizado')
        host.get_by_role('button',name='Gráfico',exact=True).click()
        graph=host.locator('[data-dex-chart]');canvas=graph.locator('[data-real-chart-canvas]')
        expected='1' if fixture['outcome']=='EXECUTED' else '0'
        expect(canvas).to_have_attribute('data-fill-count',expected)
        expect(canvas).to_have_attribute('data-buy-count',expected)
        expect(canvas).to_have_attribute('data-candle-count','0')
        if fixture['outcome']=='EXECUTED':
            graph.locator('[data-real-chart-fill]').select_option(fixture['source_id'])
            expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('Asiento')
            expect(graph.locator('[data-real-chart-state]')).to_contain_text('1 fills sin OHLC')
        graph.screenshot(path=str(output/'call-chart.png'))
    book=page.request.get(BASE+'/__test/book').json();expected_count=4 if fixture['outcome']=='EXECUTED' else 3
    assert book['events']==expected_count,book
    assert book['order_proofs']==(1 if fixture['outcome']=='EXECUTED' else 0)
    assert Decimal(book['cash']['BNB'])==Decimal('0.01')-Decimal(lost[0]['receipt']['gas_fee_wei'])/10**18
    assert Decimal(book['cash']['USDT'])==Decimal('100')-(Decimal(fixture['amount_units'])/10**18 if fixture['outcome']=='EXECUTED' else 0)
    assert bool(book['positions'])==(fixture['outcome']=='EXECUTED')
    host.screenshot(path=str(output/'call-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    # Lightweight Charts autoSize settles through ResizeObserver and animation frames.
    page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+1',timeout=3000)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    host.screenshot(path=str(output/'call-mobile.png'));page.set_viewport_size({'width':1440,'height':1050})
    page.locator('#real-lock').click();unlock(page);host,row,panel=controls()
    expect(row).to_contain_text('Confirmada' if fixture['outcome']=='EXECUTED' else 'Cancelada')
    expect(row.get_by_role('button',name='Revisar resultado',exact=True)).not_to_be_visible()
    assert page.request.get(BASE+'/__test/book').json()==book
    assert page.evaluate("!(window.fixtureMethods||[]).some(method=>method==='eth_sendTransaction'||method==='eth_sendRawTransaction')")
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})').lower()
    assert fixture['wallet'].lower() not in storage and fixture['tx_hash'].lower() not in storage
    assert not errors,errors
    (output/'result.json').write_text(json.dumps({'passed':True,'kind':fixture['kind'],'outcome':fixture['outcome'],
        'lost_confirmation_recovered_by_get':True,'wallet_sends':0,'reopened_without_reactivation':True,
        'booked_markers_without_invented_ohlc':fixture['kind']=='OPERATOR','book':book,'errors':errors},indent=2)+'\n')
    page.locator('#real-lock').click();browser.close()
print('Owned call review, one confirmation, GET recovery, accounting and encrypted reopen passed')
