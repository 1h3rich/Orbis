"""Navegador → revisión → OCO → auditorías/recibo, sin mainnet ni fondos."""

import json
import os

from playwright.sync_api import expect,sync_playwright

from tests.grvt_oco_ui_fixture import PASSWORD
from tests.test_grvt_testnet_protection import KEY,SIGNER
from tests.test_grvt_testnet_journal import SUB


BASE = 'http://127.0.0.1:8799'
cancel_flow = os.environ.get('ORBIS_TEST_OCO_CANCEL') == '1'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440,'height':1000})
    errors = []
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    page.goto(BASE+'/ui/#/real')
    page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel = page.locator('#real-grvt-oco')
    expect(panel).to_be_visible()
    panel.locator(':scope > summary').click()
    panel.locator('[name="sub_account_id"]').fill(SUB)
    panel.get_by_role('button',name='Consultar registro',exact=True).click()
    expect(panel.locator('[name="source"] option').nth(1)).to_be_attached()
    panel.locator('[name="source"]').select_option(index=1)
    panel.locator('[name="stop_loss"]').fill('49000')
    panel.locator('[name="take_profit"]').fill('53000')
    panel.locator('[name="max_order_notional"]').fill('1000')
    panel.locator('[name="api_key"]').fill('fake-testnet-key')
    panel.get_by_role('button',name='Preparar',exact=True).click()
    expect(panel.locator('[data-oco-preview]')).to_contain_text('48877.5')
    expect(panel.locator('[data-oco-preview]')).to_contain_text('52867.5')
    expect(panel.locator('[name="api_key"]')).to_have_value('')
    panel.locator('[name="api_key"]').fill('fake-testnet-key')
    panel.locator('[name="dedicated_private_key"]').fill(KEY.hex())
    panel.locator('[name="expected_signer"]').fill(SIGNER)
    panel.get_by_role('button',name='Enviar · TESTNET',exact=True).click()
    group = panel.locator('[data-oco-group]').first
    expect(group).to_be_visible()
    expected = 'Envío incierto' if os.environ.get('ORBIS_TEST_OCO_LOST_REPLY') == '1' else 'Acuse recibido'
    expect(group).to_contain_text(expected)
    assert page.request.get(BASE+'/__test/state').json()['bulk_sends'] == 1
    expect(panel.locator('[name="dedicated_private_key"]')).to_have_value('')
    panel.locator('[name="api_key"]').fill('fake-testnet-key')
    group.get_by_role('button',name='Consultar',exact=True).click()
    expect(group).to_contain_text('OPEN')
    if cancel_flow:
        panel.locator('[name="api_key"]').fill('fake-testnet-key')
        group.get_by_role('button',name='Cancelar salidas',exact=True).click()
        expect(group).to_contain_text('Cancelación ACKNOWLEDGED')
        expect(panel.locator('[data-oco-page]')).to_contain_text('STOP activo')
    else:
        assert page.request.post(BASE+'/__test/trigger').ok
        panel.locator('[name="api_key"]').fill('fake-testnet-key')
        group.get_by_role('button',name='Consultar',exact=True).click()
        expect(group).to_contain_text('FILLED')
    for i in range(2):
        if i: page.wait_for_timeout(5100)
        panel.locator('[name="api_key"]').fill('fake-testnet-key')
        group.get_by_role('button',name='Auditar',exact=True).click()
        expect(group).to_contain_text('Auditorías '+str(i+1))
    expect(group.get_by_role('button',name='Conciliar',exact=True)).to_be_enabled()
    group.get_by_role('button',name='Conciliar',exact=True).click()
    expect(group).to_contain_text('Conciliada')
    expect(group).to_contain_text('Neto de salidas · '+('0' if cancel_flow else '29.93')+' USDT')
    state = page.request.get(BASE+'/__test/state').json()
    assert state['position'] in (('0.01',) if cancel_flow else ('0.00','0'))
    assert state['cancel_sends'] == (1 if cancel_flow else 0)
    expect(group).to_contain_text('Posición restante · '+('BTC_USDT_Perp · 0.01' if cancel_flow else 'plana'))
    storage = page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (KEY.hex(),SIGNER,'fake-testnet-key'):
        assert secret not in storage
    panel.scroll_into_view_if_needed()
    page.screenshot(path='/tmp/orbis-grvt-oco-ui.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    page.screenshot(path='/tmp/orbis-grvt-oco-mobile.png',full_page=True)
    page.locator('#real-lock').click()
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel = page.locator('#real-grvt-oco')
    panel.locator(':scope > summary').click()
    panel.locator('[name="sub_account_id"]').fill(SUB)
    panel.get_by_role('button',name='Consultar registro',exact=True).click()
    expect(panel.locator('[data-oco-group]')).to_contain_text('Conciliada')
    assert page.request.get(BASE+'/__test/state').json()['bulk_sends'] == 1
    page.locator('#real-hide').click()
    panel = page.locator('#real-grvt-oco')
    panel.locator(':scope > summary').click()
    expect(panel.locator('[data-oco-state]')).to_contain_text('Importes ocultos')
    expect(panel.get_by_role('button',name='Preparar',exact=True)).to_be_disabled()
    assert not errors,errors
    browser.close()
    print('OCO UI: preparación, firma, un bulk, observación, finanzas y reapertura correctos; móvil y secretos comprobados')
