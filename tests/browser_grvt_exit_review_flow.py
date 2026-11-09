"""Evaluación puntual visible de salidas reales del motor; sólo GRVT ficticio."""

from playwright.sync_api import sync_playwright,expect

from tests.grvt_bracket_ui_fixture import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER


BASE='http://127.0.0.1:8799'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100}); errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    source=page.request.get(BASE+'/__test/state').json()['source']
    key='fake-testnet-key-'+source['sub_account_id']
    page.goto(BASE+'/ui/#/real'); page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-bracket'); panel.locator(':scope > summary').click()
    expect(panel.locator('[name="source"] option').nth(1)).to_be_attached()
    panel.locator('[name="source"]').select_option(source['client_order_id'])
    for name,value in {'sub_account_id':source['sub_account_id'],'size':'0.5','limit_price':'100',
        'stop_loss':'95','take_profit':'110','entry_cap':'100','exit_cap':'100','api_key':key}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.get_by_role('button',name='Revisar entrada',exact=True).click()
    expect(panel.locator('[data-bracket-review]')).to_be_visible()
    panel.locator('[name="api_key"]').fill(key)
    panel.locator('[name="dedicated_private_key"]').fill(KEY.hex())
    panel.locator('[name="expected_signer"]').fill(SIGNER)
    panel.get_by_role('button',name='Firmar entrada · TESTNET',exact=True).click()
    group=panel.locator('[data-bracket-group]').first; expect(group).to_contain_text('Acuse recibido')
    assert page.request.post(BASE+'/__test/lock-sources').ok
    panel.locator('[name="api_key"]').fill(key)
    group.get_by_role('button',name='Evaluar salidas',exact=True).click()
    result=group.locator('[data-strategy-exit-review]')
    expect(result).to_contain_text('Salidas calculadas · Mantener')
    expect(result).to_contain_text('Entrada ejecutada · 100 · 0.5')
    expect(result).to_contain_text('SL · 97.00 / TP · 108.00')
    expect(result).to_contain_text('Consulta puntual')
    expect(result).to_contain_text('no ejecuta')
    expect(panel.locator('[name="api_key"]')).to_have_value('')
    expect(panel.locator('[name="dedicated_private_key"]')).to_have_value('')
    state=page.request.get(BASE+'/__test/state').json()
    assert state['bulk_sends']==1 and state['cancel_sends']==state['close_sends']==0
    assert state['position']=='0.5'
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    result.scroll_into_view_if_needed(); page.screenshot(path='/tmp/orbis-filled-exit-mobile.png')
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (key,KEY.hex(),SIGNER): assert secret not in storage
    page.locator('#real-lock').click()
    expect(page.locator('[data-strategy-exit-review]')).to_have_count(0)
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-bracket'); panel.locator(':scope > summary').click()
    panel.locator('[name="api_key"]').fill(key)
    group=panel.locator('[data-bracket-group]').first
    group.get_by_role('button',name='Evaluar salidas',exact=True).click()
    expect(group.locator('[data-strategy-exit-review]')).to_contain_text('Entrada ejecutada · 100')
    assert page.request.get(BASE+'/__test/state').json()['bulk_sends']==1
    assert not errors,errors
    browser.close()
