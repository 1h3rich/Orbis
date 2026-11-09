"""Recover an IOC whose acknowledgement was lost, using View after reopening."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.test_grvt_mainnet_submit import PASSWORD


base = os.environ['ORBIS_TEST_BASE_URL']
output = Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
history_fallback = os.environ.get('ORBIS_TEST_GRVT_VIEW_IOC_HISTORY') == '1'


def open_history(page):
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button', name='Desbloquear', exact=True).click()
    panel = page.locator('#real-grvt-mainnet')
    panel.locator(':scope > summary').click()
    panel.locator('[name="sub_account_id"]').fill('123456')
    panel.locator('[data-mainnet-refresh]').click()
    group = panel.locator('[data-mainnet-group]').first
    expect(group).to_be_visible(timeout=30000)
    return panel, group


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440, 'height':1000})
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST'
            and request.url.endswith(('/send','/recover','/renew','/rearm')) else None)
    before = page.request.get(base + '/__test/state').json()
    assert before['fixture_id'] == output.name and before['reductions'] == 1 and before['cancels'] == 0
    page.goto(base + '/ui/#/real')
    panel, group = open_history(page)
    expect(group).to_contain_text('Ejecución sin confirmar')
    expect(group.locator('[data-mainnet-consult]')).to_be_disabled()
    panel.locator('[data-mainnet-view-credentials] summary').click()
    key = panel.locator('[name="view_api_key"]')
    key.fill('synthetic-mainnet-view-key')
    with page.expect_response('**/mainnet/connect-view') as view:
        panel.locator('[data-mainnet-connect-view]').click()
    assert view.value.ok and view.value.json()['can_sign'] is False
    expect(key).to_have_value('')
    expect(group.locator('[data-mainnet-close-position]')).to_be_disabled()
    group.locator('[data-mainnet-consult]').click()
    expect(group).to_contain_text('Cierre IOC · FILLED · 0.3', timeout=30000)
    expect(group).to_contain_text('Neto conciliado · 1.96 USDT', timeout=45000)
    expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
    page.locator('#real-lock').click()
    panel, group = open_history(page)
    expect(group).to_contain_text('Neto conciliado · 1.96 USDT')
    after = page.request.get(base + '/__test/state').json()
    assert (after['sends'],after['reductions'],after['cancels']) == (before['sends'],1,0)
    assert (after['order_history_requests'] >= 14) if history_fallback else after['order_history_requests'] == 0
    assert after['jobs'] == {} and not writes and not errors, (writes,errors)
    (output/'view-ioc-result.json').write_text(json.dumps({'view_only':True,'existing_ioc':1,
        'no_new_exchange_sends':True,'reopened_receipt':True,'net_usdt':'1.96',
        'history_fallback':history_fallback,'history_requests':after['order_history_requests']},indent=2))
    print('GRVT View recuperó el IOC archivado y conservó el recibo tras bloquear y reabrir.')
    browser.close()
