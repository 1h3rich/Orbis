"""New backend process recovers owned DCA and manual portfolio unchanged."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ['ORBIS_TEST_BASE_URL']
DATA = Path(os.environ['ORBIS_BASIC_DIRECTORY'])
OUT = DATA.parent
PASSWORD = 'owned portfolio dca journey password'
assert os.environ.get('ORBIS_BASIC_FIXTURE') == '1'
assert os.environ['ORBIS_BASIC_PHASE'] == 'reopen'
saved = json.loads((OUT / 'result.json').read_text())
assert saved['passed']

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, timezone_id='UTC')
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: writes.append(request.url)
            if request.method in ('POST', 'PUT', 'DELETE') else None)
    page.route('**/market/**', lambda route: route.fulfill(status=503, json={}))
    try:
        fixture = page.request.get(BASE + '/__test/state').json()
        assert fixture['fixture_id'] == OUT.name and fixture['server'] != saved['server']
        for endpoint, filename in [('/lab/status', 'lab.db'), ('/automation/dca/status', 'automation.db'),
                                   ('/real/vault/status', 'real.db')]:
            status = page.request.get(BASE + endpoint).json()
            assert status['unlocked'] is False, status
            assert (DATA / filename).read_bytes()[:16] != b'SQLite format 3\x00'
        assert page.request.get(BASE + '/automation/dca/rules').status == 423
        assert page.request.get(BASE + '/real/accounts').status == 401
        page.goto(BASE + '/ui/#/config')
        page.wait_for_load_state('networkidle')
        for selector, endpoint in [('#cfg-lab-unlock-form', '/lab/unlock'),
                                   ('#cfg-automation-unlock-form', '/automation/dca/unlock')]:
            form = page.locator(selector)
            form.evaluate("el=>{for(let p=el.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}}")
            expect(form).to_be_visible()
            expect(form.locator('[name=session_seconds]')).to_have_value('900')
            form.locator('[name="passphrase"]').fill(PASSWORD)
            with page.expect_response(BASE + endpoint) as response:
                form.locator('button[type="submit"]').click()
            assert response.value.status == 200, response.value.text()
        page.goto(BASE + '/ui/#/dca')
        page.wait_for_load_state('networkidle')
        page.locator('#dca-rule').select_option(saved['rule']['id'])
        expect(page.locator('#dca-version')).to_have_value(saved['rule']['version_id'])
        page.locator('#dca-run-select').select_option(saved['run']['id'])
        page.locator('#dca-report .lab-verify button').click()
        expect(page.locator('#dca-report .lab-verify')).to_have_attribute('data-state', 'verified')
        assert page.request.get(BASE + '/automation/dca/runs/' + saved['run']['id']).json() == saved['run']
        identifier = saved['paper']['id']
        page.locator('#dca-paper-session').select_option(identifier)
        expect(page.locator('#dca-paper-report .statusbadge')).to_contain_text('STOPPED')
        expect(page.locator('#dca-paper-report')).to_contain_text('BUY')
        recovered = page.request.get(BASE + '/automation/dca/paper/sessions/' + identifier).json()
        assert recovered == saved['paper']
        sessions = page.request.get(BASE + '/automation/dca/paper/sessions').json()
        assert len(sessions) == 1 and sessions[0]['status'] == 'STOPPED'

        page.goto(BASE + '/ui/#/real')
        page.wait_for_load_state('networkidle')
        page.locator('#real-passphrase').fill(PASSWORD)
        with page.expect_response(BASE + '/real/vault/unlock') as response:
            page.get_by_role('button', name='Desbloquear', exact=True).click()
        assert response.value.status == 200, response.value.text()
        headers = {'X-Orbis-Vault-Token': response.value.json()['token']}
        expect(page.locator('#real-account')).to_have_value(saved['account']['id'])
        expect(page.locator('#real-history tbody tr')).to_have_count(6)
        prefix = BASE + '/real/accounts/' + saved['account']['id']
        assert page.request.get(prefix + '/snapshot', headers=headers).json() == saved['snapshot']
        assert page.request.get(prefix + '/events', headers=headers).json() == saved['events']
        assert PASSWORD not in str(page.evaluate('Object.values(localStorage)'))
        assert not any(url.endswith('/events') or url.endswith('/sessions') for url in writes), writes
        assert not any(any(fragment in url for fragment in ('/exchanges/', '/mexc-', '/dex-', '/wallet-')) for url in writes), writes
        assert not errors, errors
        page.screenshot(path=str(OUT / 'reopened-desktop.png'), full_page=True)
        page.locator('#real-lock').click()
        expect(page.locator('#real-passphrase')).to_be_visible()
        (OUT / 'reopened.json').write_text(json.dumps({'passed': True, 'server': fixture['server'],
            'source_server': saved['server'], 'run_id': saved['run']['id'],
            'session_id': identifier, 'account_id': saved['account']['id'],
            'event_count': len(saved['events']), 'errors': errors}, indent=2))
    except Exception:
        page.screenshot(path=str(OUT / 'reopen-failure.png'), full_page=True)
        (OUT / 'reopen-failure-dom.yml').write_text(page.locator('body').aria_snapshot())
        raise
    finally:
        browser.close()

print('Restart: encrypted DCA/portfolio locked; exact results, trades, costs and stopped session recovered')
