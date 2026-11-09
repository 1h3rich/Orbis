"""Reopen owned encrypted LAB/PAPER after a genuine backend process restart."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ['ORBIS_TEST_BASE_URL']
OUT = Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
assert os.environ.get('ORBIS_BASIC_FIXTURE') == '1'
assert os.environ['ORBIS_BASIC_PHASE'] == 'reopen'
saved = json.loads((OUT / 'result.json').read_text())
assert saved['passed']

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1100}, timezone_id='UTC')
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: writes.append((request.method, request.url))
            if request.method in ('POST', 'PUT', 'DELETE') else None)
    try:
        fixture = page.request.get(BASE + '/__test/state').json()
        assert fixture['fixture_id'] == OUT.name and not fixture['real_exists']
        assert fixture['server'] != saved['server']
        assert fixture['worker']['running']
        for endpoint, file in [('/lab', 'lab.db'), ('/paper/spot/strategies', 'spot.db')]:
            status = page.request.get(BASE + endpoint + '/status').json()
            assert status['unlocked'] is False, status
            assert (OUT / 'data' / file).read_bytes()[:16] != b'SQLite format 3\x00'
        assert page.request.get(BASE + '/lab/runs').status == 423
        assert page.request.get(BASE + '/paper/spot/strategies').status == 423
        page.goto(BASE + '/ui/#/config')
        page.wait_for_load_state('networkidle')
        for selector, endpoint in [('#cfg-lab-unlock-form', '/lab/unlock'),
                                   ('#cfg-spot-unlock-form', '/paper/spot/strategies/unlock')]:
            form = page.locator(selector)
            form.evaluate("el=>{for(let p=el.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}}")
            expect(form).to_be_visible()
            expect(form.locator('[name=session_seconds]')).to_have_value('900')
            form.locator('[name="passphrase"]').fill('owned basic journey password')
            with page.expect_response(BASE + endpoint) as response:
                form.locator('button[type="submit"]').click()
            assert response.value.status == 200, response.value.text()

        page.goto(BASE + '/ui/#/laboratorio')
        page.wait_for_load_state('networkidle')
        run = saved['long']
        page.locator('#lab-run-select').select_option(run['id'])
        expect(page.locator('#lab-run-verify')).to_have_attribute('data-state', 'verified')
        expect(page.locator('#lab-paper-action')).to_be_visible()
        expect(page.locator('#lab-chart')).to_have_attribute('data-entry-count', str(run['metrics']['trades']))
        recovered = page.request.get(BASE + '/lab/runs/' + run['id']).json()
        assert recovered['version_id'] == saved['strategy']['version_id']
        for key in ('metrics', 'dataset_hash', 'strategy_hash', 'result_hash', 'trades'):
            assert recovered[key] == saved['long_full'][key], key
        assert page.request.get(BASE + '/lab/runs/' + saved['short']['id'] + '/verify').json()['verified']

        # Navigation retains the old LONG version even when v2 is SHORT.
        # Opening the handoff and recovering an existing session must not
        # create or activate a second session.
        page.locator('#lab-paper-action').click()
        expect(page.locator('#spot-paper-version')).to_have_value(saved['strategy']['version_id'])
        identifier = saved['paper']['id']
        page.locator('#spot-paper-session').select_option(identifier)
        expect(page.locator('#spot-paper-chart')).to_have_attribute('data-buy-count', '1')
        expect(page.locator('#spot-paper-chart')).to_have_attribute('data-sell-count', '1')
        sessions = page.request.get(BASE + '/paper/spot/strategies').json()
        assert len(sessions) == 1 and sessions[0]['status'] == 'STOPPED'
        detail = page.request.get(BASE + '/paper/spot/strategies/' + identifier).json()
        assert detail['state'] == saved['paper']['state']
        assert detail['events'] == saved['paper']['events']
        check = page.request.get(BASE + '/paper/spot/strategies/' + identifier + '/verify')
        assert check.status == 200 and check.json()['verified'], check.text()
        assert not any('/real/' in url for _, url in writes), writes
        assert not any(url.endswith('/paper/spot/strategies') for _, url in writes), writes
        assert not page.request.get(BASE + '/__test/state').json()['real_exists']
        assert not errors, errors
        page.screenshot(path=str(OUT / 'reopened-desktop.png'), full_page=True)
        (OUT / 'reopened.json').write_text(json.dumps({'passed': True,
            'server': fixture['server'], 'source_server': saved['server'],
            'run_id': run['id'], 'session_id': identifier, 'verified': check.json(),
            'errors': errors}, indent=2))
    except Exception:
        page.screenshot(path=str(OUT / 'reopen-failure.png'), full_page=True)
        (OUT / 'reopen-failure-dom.yml').write_text(page.locator('body').aria_snapshot())
        raise
    finally:
        browser.close()

print('Restart: encrypted bases locked, exact LAB/PAPER recovered, no automatic activation')
