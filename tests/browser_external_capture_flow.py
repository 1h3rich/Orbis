"""Manual pending history -> actual external receipt/order -> reviewed gas -> cold UI."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure
from tests.test_wallet_swap import PASSWORD

BASE=os.environ['ORBIS_TEST_BASE_URL'];out=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright()as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==out.name and fixture['mode']=='CAPTURE'
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    page.get_by_role('button',name='Conectar wallet',exact=True).click();expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
    if page.locator('#real-account').input_value()!=fixture['account_id']:page.locator('#real-account').select_option(fixture['account_id'])
    page.get_by_role('button',name='Verificar control',exact=True).click();expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    before=page.request.get(BASE+'/__test/book').json();assert before['books']==before['acceptances']==0
    panel=page.locator('#real-wallet-swap');panel.locator(':scope > details > summary').click()
    panel.get_by_text('Historial DEX',exact=True).click();panel.locator('[data-swap-history-refresh]').click()
    expect(panel.locator('[data-swap-external]')).to_have_count(1)
    expect(panel.locator('[data-swap-hash]')).to_have_value('')
    with page.expect_response('**/dex-call-resolutions/*/*/discover') as found:
        panel.get_by_role('button',name='Revisar resultado',exact=True).click()
    assert found.value.status==200,found.value.text()
    assert found.value.json()=={'external_review_required':True,'discovered_tx_hash':fixture['hashes'][0]}
    dialog=page.get_by_role('dialog',name='Recibos externos',exact=True)
    expect(dialog.get_by_role('button',name='Guardar recibo',exact=True)).to_be_visible()
    dialog.get_by_role('button',name='Guardar recibo',exact=True).click();expect(dialog.get_by_role('button',name='Guardar bloque',exact=True)).to_be_visible()
    dialog.get_by_role('button',name='Guardar bloque',exact=True).click();expect(dialog.get_by_role('button',name='Sin movimiento',exact=True)).to_be_visible()
    dialog.get_by_role('button',name='Sin movimiento',exact=True).click()
    with page.expect_response('**/external-effects/preview')as unknown:dialog.get_by_role('button',name='Revisar selección',exact=True).click()
    assert unknown.value.status==200,unknown.value.text();assert not unknown.value.json()['manual_review_complete']
    expect(dialog.get_by_role('button',name='Confirmar registro',exact=True)).to_be_disabled()
    dialog.get_by_role('button',name='Volver',exact=True).click();dialog.get_by_label('Recibo revisado',exact=True).check()
    with page.expect_response('**/external-effects/preview')as preview:dialog.get_by_role('button',name='Revisar selección',exact=True).click()
    assert preview.value.status==200,preview.value.text();assert preview.value.json()['events_to_create']==1
    dialog.screenshot(path=str(out/'capture-comparison.png'))
    dialog.get_by_role('button',name='Confirmar registro',exact=True).click();expect(dialog.locator('.economic-saved')).to_have_text('Registro guardado ✓')
    after=page.request.get(BASE+'/__test/book').json();assert after['events']==before['events']+1 and after['books']==after['acceptances']==1 and after['groups']==0
    assert after['originals_preserved']
    dialog.get_by_role('button',name='Cerrar',exact=True).click();assert page.locator('[data-external-body]').inner_text()==''
    page.locator('#real-lock').click();page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    if page.locator('#real-account').input_value()!=fixture['account_id']:page.locator('#real-account').select_option(fixture['account_id'])
    page.get_by_role('button',name='Recibos externos',exact=True).click();dialog.get_by_role('button',name='Ver registro',exact=True).click()
    expect(dialog.locator('.economic-saved')).to_have_text('Registro guardado ✓')
    assert not page.evaluate('(fixtureMethods||[]).some(m=>/send|signTransaction|signTypedData/.test(m))')
    assert not errors,errors
    (out/'result.json').write_text(json.dumps({'passed':True,'actual_receipt_capture':True,'saved_external_order':True,'unknown_blocks_confirmation':True,'events_created':1,'cold_without_wallet':True,'wallet_sends':0,'errors':errors},indent=2))
    browser.close()
